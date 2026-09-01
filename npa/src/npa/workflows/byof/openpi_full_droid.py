"""Prepare and run pinned full-DROID pi0.5 fine-tuning on eight GPU nodes."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.metadata
import inspect
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from npa.workflows.byof.openpi_pipeline import (
    OpenPIPipelineError,
    _load_upstream_train_module,
    _read_json_uri,
    _read_bytes_uri,
    _redistribution_evidence,
    _source_build_evidence,
    _upload_checkpoint,
    _validate_runtime_image,
    _uri_exists,
    _write_bytes_uri,
    _write_json_uri,
)

SOURCE_REF = "15a9616a00943ada6c20a0f158e3adb39df2ccac"
CONFIG_NAME = "pi05_full_droid_finetune"
DATASET_URI = "gs://gresearch/robotics/droid/1.0.1"
EXPECTED_STEPS = 100_000
EXPECTED_BATCH_SIZE = 256
EXPECTED_PROCESSES = 8
EXPECTED_LOCAL_DEVICES = 1
EXPECTED_DEVICES = EXPECTED_PROCESSES * EXPECTED_LOCAL_DEVICES
EXPECTED_FSDP_DEVICES = EXPECTED_DEVICES
NORM_MAX_FRAMES = 10_000_000
COORDINATOR_PORT = 29601
TELEMETRY_SCHEMA = "npa.workbench.openpi.pi05-full-droid-telemetry.v1"
RERUN_APPLICATION_ID = "npa_openpi_pi05_full_droid"
RERUN_TIMELINE = "optimizer_step"
RERUN_SCHEMA = "application/vnd.rerun.rrd"
REQUIRED_RRD_ENTITIES = (
    "metrics/loss",
    "metrics/learning_rate",
    "health/gradient_norm",
    "health/param_norm",
    "health/gradient_to_parameter_ratio",
    "health/nonfinite",
    "timing/interval_seconds",
    "throughput/optimizer_steps_per_second",
    "throughput/global_samples_per_second",
    "checkpoint/save_requested",
    "checkpoint/materialized",
    "health/distributed/process_count",
    "health/distributed/global_devices",
    "health/distributed/local_devices_per_process",
    "health/distributed/distinct_nodes",
    "health/device/sm120_ranks",
    "provenance/run",
)
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _scalar(value: object, *, name: str) -> float:
    import numpy as np

    array = np.asarray(value)
    if array.size != 1:
        raise OpenPIPipelineError(f"telemetry {name} is not scalar")
    result = float(array.reshape(()))
    if not math.isfinite(result):
        raise OpenPIPipelineError(f"telemetry {name} is not finite")
    return result


def _load_telemetry_records(path: Path, *, run_id: str) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OpenPIPipelineError(
                f"telemetry journal line {number} is invalid JSON"
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("schema") != TELEMETRY_SCHEMA
            or value.get("run_id") != run_id
        ):
            raise OpenPIPipelineError(
                f"telemetry journal line {number} has incompatible provenance"
            )
        optimizer_step = value.get("optimizer_step")
        segment = value.get("segment")
        if (
            isinstance(optimizer_step, bool)
            or not isinstance(optimizer_step, int)
            or optimizer_step < 0
            or isinstance(segment, bool)
            or not isinstance(segment, int)
            or segment < 1
        ):
            raise OpenPIPipelineError(
                f"telemetry journal line {number} has invalid step or segment"
            )
        records.append(value)
    return records


class _TrainingTelemetryJournal:
    """Durable rank-zero facts captured around the pinned upstream trainer."""

    def __init__(self, path: Path, *, run_id: str, config: object) -> None:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise OpenPIPipelineError(f"unsafe telemetry run id {run_id!r}")
        self.path = path
        self.run_id = run_id
        self.config = config
        self.path.parent.mkdir(parents=True, exist_ok=True)
        records = _load_telemetry_records(path, run_id=run_id)
        self._metric_steps = {
            int(record["optimizer_step"])
            for record in records
            if record.get("record_type") == "metrics"
        }
        self._checkpoint_events = {
            (int(record["optimizer_step"]), str(record["event"]))
            for record in records
            if record.get("record_type") == "checkpoint"
        }
        self._has_provenance = any(
            record.get("record_type") == "provenance" for record in records
        )
        self._segment = 1 + max(
            (int(record.get("segment", 0)) for record in records), default=0
        )
        self._started = time.perf_counter()
        self._last_metric_step: int | None = None
        self._last_metric_time: float | None = None
        self._handle = self.path.open("a", encoding="utf-8")
        if not self._has_provenance:
            self._append(
                {
                    "record_type": "provenance",
                    "optimizer_step": 0,
                    "source_ref": SOURCE_REF,
                    "config_name": CONFIG_NAME,
                    "dataset_uri": DATASET_URI,
                    "optimizer_steps": int(config.num_train_steps),
                    "global_batch_size": int(config.batch_size),
                    "log_interval": int(config.log_interval),
                    "save_interval": int(config.save_interval),
                    "held_out_policy_comparison": "not_produced_by_training_run",
                }
            )
            self._has_provenance = True

    def _append(self, value: Mapping[str, object]) -> None:
        record = {
            "schema": TELEMETRY_SCHEMA,
            "run_id": self.run_id,
            "segment": self._segment,
            **value,
        }
        self._handle.write(json.dumps(record, sort_keys=True) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def record_metrics(
        self, *, step: int, values: Mapping[str, object], learning_rate: object
    ) -> None:
        if step in self._metric_steps:
            return
        required = {"loss", "grad_norm", "param_norm"}
        if not required <= values.keys():
            return
        metrics = {
            name: _scalar(values[name], name=name) for name in sorted(required)
        }
        lr = _scalar(learning_rate, name="learning_rate")
        now = time.perf_counter()
        interval: dict[str, object] | None = None
        if self._last_metric_time is not None and self._last_metric_step is not None:
            seconds = now - self._last_metric_time
            steps = step - self._last_metric_step
            if seconds <= 0 or steps <= 0:
                raise OpenPIPipelineError("telemetry interval is not monotonic")
            interval = {
                "optimizer_steps": steps,
                "seconds": seconds,
                "optimizer_steps_per_second": steps / seconds,
                "global_samples_per_second": (
                    steps * int(self.config.batch_size) / seconds
                ),
            }
        gradient_ratio = metrics["grad_norm"] / max(metrics["param_norm"], 1e-30)
        self._append(
            {
                "record_type": "metrics",
                "optimizer_step": step,
                "elapsed_segment_seconds": now - self._started,
                "metrics": {**metrics, "learning_rate": lr},
                "health": {
                    "all_finite": True,
                    "gradient_to_parameter_ratio": gradient_ratio,
                },
                "interval": interval,
            }
        )
        self._metric_steps.add(step)
        self._last_metric_step = step
        self._last_metric_time = now

    def record_checkpoint(self, *, step: int, event: str) -> None:
        identity = (step, event)
        if identity in self._checkpoint_events:
            return
        if step < 0 or event not in {"save_requested", "materialized"}:
            raise OpenPIPipelineError("invalid checkpoint telemetry event")
        self._append(
            {
                "record_type": "checkpoint",
                "optimizer_step": step,
                "event": event,
                "relative_path": f"{step}/",
                "final": step == int(self.config.num_train_steps) - 1,
            }
        )
        self._checkpoint_events.add(identity)

    def close(self) -> None:
        self._handle.close()


def _require_terms() -> None:
    if os.environ.get("NPA_OPENPI_ACCEPT_GEMMA_TERMS") != "YES":
        raise OpenPIPipelineError(
            "full-DROID pi0.5 fine-tuning requires exact run-scoped Gemma terms acceptance"
        )


def _run(
    command: Sequence[str], *, cwd: Path | None = None, stdout: object = None
) -> None:
    subprocess.run(command, cwd=cwd, check=True, stdout=stdout)  # noqa: S603


def _validate_source(repo_root: Path, runtime_image: str) -> dict[str, object]:
    _validate_runtime_image(runtime_image)
    build = _source_build_evidence(repo_root)
    source_metadata = build.get("source_metadata")
    if (
        not isinstance(source_metadata, dict)
        or source_metadata.get("ref") != SOURCE_REF
    ):
        raise OpenPIPipelineError(
            "runtime image does not contain the pinned OpenPI source"
        )
    return build


def _remote_inventory(gsutil: str, manifest_path: Path) -> dict[str, object]:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as handle:
        _run([gsutil, "ls", "-l", "-r", DATASET_URI + "/**"], stdout=handle)
    content = manifest_path.read_bytes()
    count = 0
    total = 0
    for line in content.decode("utf-8", errors="strict").splitlines():
        match = re.match(r"^\s*(\d+)\s+\d{4}-\d{2}-\d{2}T\S+\s+gs://", line)
        if match:
            count += 1
            total += int(match.group(1))
    if count < 1 or total < 1:
        raise OpenPIPipelineError("DROID 1.0.1 GCS inventory was empty or unreadable")
    return {
        "uri": DATASET_URI,
        "version": "1.0.1",
        "object_count": count,
        "total_size_bytes": total,
        "listing_sha256": hashlib.sha256(content).hexdigest(),
    }


def _local_inventory(root: Path) -> dict[str, int]:
    files = [path for path in root.rglob("*") if path.is_file()]
    return {
        "file_count": len(files),
        "total_size_bytes": sum(path.stat().st_size for path in files),
    }


def _stage_dataset(gsutil: str, data_root: Path, work_root: Path) -> dict[str, object]:
    destination = data_root / "droid" / "1.0.1"
    destination.mkdir(parents=True, exist_ok=True)
    remote = _remote_inventory(gsutil, work_root / "droid-1.0.1-gcs-listing.txt")
    _run([gsutil, "-m", "rsync", "-r", "-c", DATASET_URI, str(destination)])
    local = _local_inventory(destination)
    if local["file_count"] != remote["object_count"]:
        raise OpenPIPipelineError(
            "DROID object count differs after checksum-verified GCS synchronization"
        )
    if local["total_size_bytes"] != remote["total_size_bytes"]:
        raise OpenPIPipelineError(
            "DROID byte count differs after checksum-verified GCS synchronization"
        )
    return {**remote, **local, "local_path_role": "run_owned_durable_pvc"}


def _configured_upstream(data_root: Path, work_root: Path, experiment: str):
    from openpi.training import config as openpi_config

    config = openpi_config.get_config(CONFIG_NAME)
    if (
        config.num_train_steps != EXPECTED_STEPS
        or config.batch_size != EXPECTED_BATCH_SIZE
    ):
        raise OpenPIPipelineError("pinned upstream full-DROID recipe drifted")
    data = dataclasses.replace(config.data, rlds_data_dir=str(data_root))
    return dataclasses.replace(
        config,
        data=data,
        assets_base_dir=str(work_root / "assets"),
        checkpoint_base_dir=str(work_root / "checkpoints"),
        exp_name=experiment,
        fsdp_devices=EXPECTED_FSDP_DEVICES,
        wandb_enabled=False,
    )


def _compute_norm_stats(config: object, repo_root: Path) -> dict[str, object]:
    from openpi.shared import normalize
    from openpi.training import config as openpi_config

    data_config = config.data.create(config.assets_dirs, config.model)
    stats_path = config.assets_dirs / data_config.repo_id / "norm_stats.json"
    if not stats_path.is_file():
        module_path = repo_root / "scripts" / "compute_norm_stats.py"
        namespace: dict[str, object] = {
            "__file__": str(module_path),
            "__name__": "npa_openpi_norm",
        }
        original = openpi_config.get_config
        try:
            openpi_config.get_config = lambda name: (
                config if name == CONFIG_NAME else original(name)
            )
            exec(compile(module_path.read_bytes(), str(module_path), "exec"), namespace)  # noqa: S102
            namespace["main"](CONFIG_NAME, max_frames=NORM_MAX_FRAMES)  # type: ignore[operator]
        finally:
            openpi_config.get_config = original
    if not stats_path.is_file():
        raise OpenPIPipelineError("normalization statistics were not materialized")
    loaded = normalize.load(stats_path.parent)
    if not loaded:
        raise OpenPIPipelineError("normalization statistics are empty")
    return {
        "path_role": "run_owned_durable_pvc",
        "max_frames": NORM_MAX_FRAMES,
        "sha256": hashlib.sha256(stats_path.read_bytes()).hexdigest(),
    }


def _prepare(args: argparse.Namespace) -> int:
    _require_terms()
    repo_root = Path(args.repo_root)
    build = _validate_source(repo_root, args.runtime_image)
    work_root = Path(args.work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    dataset = _stage_dataset(args.gsutil, Path(args.data_root), work_root)
    config = _configured_upstream(Path(args.data_root), work_root, args.experiment)
    normalization = _compute_norm_stats(config, repo_root)
    result: dict[str, object] = {
        "schema": "npa.workbench.openpi.pi05-full-droid-prepare.v1",
        "status": "passed",
        "source": {
            "repository": "https://github.com/Physical-Intelligence/openpi",
            "ref": SOURCE_REF,
            "license": "Apache-2.0",
            **build,
        },
        "dataset": dataset,
        "normalization": normalization,
        "recipe": {
            "config_name": CONFIG_NAME,
            "global_batch_size": EXPECTED_BATCH_SIZE,
            "optimizer_steps": EXPECTED_STEPS,
            "normalization_max_frames": NORM_MAX_FRAMES,
        },
        "terms": {"forwarded": True, "persisted": False},
        "timings_seconds": {"total": round(time.perf_counter() - started, 3)},
    }
    _write_json_uri(args.output_uri, result)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


def _multihost_environment() -> tuple[int, list[str]]:
    try:
        rank = int(os.environ["SKYPILOT_NODE_RANK"])
        num_nodes = int(os.environ["SKYPILOT_NUM_NODES"])
    except (KeyError, ValueError) as exc:
        raise OpenPIPipelineError(
            "SkyPilot multi-node rank metadata is required"
        ) from exc
    node_ips = [
        value.strip()
        for value in os.environ.get("SKYPILOT_NODE_IPS", "").splitlines()
        if value.strip()
    ]
    if num_nodes != EXPECTED_PROCESSES or len(node_ips) != EXPECTED_PROCESSES:
        raise OpenPIPipelineError(
            f"full-DROID requires {EXPECTED_PROCESSES} SkyPilot nodes, got {num_nodes} and {len(node_ips)} IPs"
        )
    if rank not in range(EXPECTED_PROCESSES):
        raise OpenPIPipelineError(f"invalid SkyPilot node rank {rank}")
    visible = os.environ.get("SKYPILOT_NUM_GPUS_PER_NODE", "")
    if visible and int(float(visible)) != EXPECTED_LOCAL_DEVICES:
        raise OpenPIPipelineError(
            f"expected one GPU per node, SkyPilot reported {visible}"
        )
    return rank, node_ips


def _initialize_multihost(rank: int, node_ips: Sequence[str]) -> object:
    import jax
    from jax.experimental import multihost_utils

    jax.distributed.initialize(
        coordinator_address=f"{node_ips[0]}:{COORDINATOR_PORT}",
        num_processes=EXPECTED_PROCESSES,
        process_id=rank,
        local_device_ids=[0],
    )
    if jax.process_count() != EXPECTED_PROCESSES or jax.process_index() != rank:
        raise OpenPIPipelineError(
            "JAX process topology differs from the SkyPilot topology"
        )
    if (
        jax.device_count() != EXPECTED_DEVICES
        or jax.local_device_count() != EXPECTED_LOCAL_DEVICES
    ):
        raise OpenPIPipelineError(
            f"expected {EXPECTED_DEVICES} global and one local GPU, got "
            f"{jax.device_count()} global and {jax.local_device_count()} local"
        )
    multihost_utils.sync_global_devices("npa-openpi-initialized")
    return multihost_utils


def _install_distributed_rlds_adapter(rank: int) -> None:
    """Adapt the pinned RLDS loader without changing the upstream recipe."""

    import dlimp as dl
    import jax
    import tensorflow as tf
    from openpi.training import data_loader

    tf.random.set_seed(42 + rank)
    original_sample = dl.DLataset.sample_from_datasets

    def sharded_sample(*args, **kwargs):
        dataset = original_sample(*args, **kwargs)
        return dataset.shard(EXPECTED_PROCESSES, rank)

    dl.DLataset.sample_from_datasets = sharded_sample
    original_create = data_loader.create_rlds_dataset

    def create_local_rlds(data_config, action_horizon, batch_size, *, shuffle=False):
        if batch_size % EXPECTED_PROCESSES:
            raise OpenPIPipelineError(
                "global RLDS batch is not divisible by the process count"
            )
        return original_create(
            data_config,
            action_horizon,
            batch_size // EXPECTED_PROCESSES,
            shuffle=shuffle,
        )

    data_loader.create_rlds_dataset = create_local_rlds

    def rlds_init(self, dataset, *, sharding=None, num_batches=None):
        if sharding is None:
            sharding = jax.sharding.NamedSharding(
                jax.sharding.Mesh(jax.devices(), ("B",)),
                jax.sharding.PartitionSpec("B"),
            )
        self._dataset = dataset
        self._sharding = sharding
        self._num_batches = num_batches

    data_loader.RLDSDataLoader.__init__ = rlds_init


def _checkpoint_root(config: object) -> Path:
    return Path(config.checkpoint_base_dir) / config.name / config.exp_name


def _run_training(
    config: object, repo_root: Path, *, rank: int, run_id: str
) -> tuple[Path, bool, Path | None]:
    checkpoint_root = _checkpoint_root(config)
    resuming = checkpoint_root.is_dir() and any(checkpoint_root.iterdir())
    configured = dataclasses.replace(config, resume=resuming, overwrite=not resuming)
    upstream_train = _load_upstream_train_module(repo_root)
    journal: _TrainingTelemetryJournal | None = None
    original_log = None
    original_save = None
    if rank == 0:
        journal = _TrainingTelemetryJournal(
            Path(config.checkpoint_base_dir)
            / config.name
            / config.exp_name
            / "npa-training-telemetry.jsonl",
            run_id=run_id,
            config=configured,
        )
        learning_rate = configured.lr_schedule.create()
        original_log = upstream_train.wandb.log
        original_save = upstream_train._checkpoints.save_state
        save_parameters = tuple(inspect.signature(original_save).parameters)
        if save_parameters != ("checkpoint_manager", "state", "data_loader", "step"):
            raise OpenPIPipelineError(
                "pinned upstream checkpoint callback signature drifted"
            )

        def telemetry_log(data, *positional, **keywords):
            result = original_log(data, *positional, **keywords)
            step = keywords.get("step")
            if step is None and positional:
                step = positional[0]
            if isinstance(data, Mapping) and step is not None:
                journal.record_metrics(
                    step=int(step),
                    values=data,
                    learning_rate=learning_rate(int(step)),
                )
            return result

        def telemetry_save(checkpoint_manager, state, data_loader, step):
            result = original_save(checkpoint_manager, state, data_loader, step)
            journal.record_checkpoint(step=int(step), event="save_requested")
            return result

        upstream_train.wandb.log = telemetry_log
        upstream_train._checkpoints.save_state = telemetry_save
    try:
        upstream_train.main(configured)
        if journal is not None:
            for path in checkpoint_root.iterdir():
                if path.is_dir() and path.name.isdigit():
                    journal.record_checkpoint(
                        step=int(path.name), event="materialized"
                    )
    finally:
        if original_log is not None:
            upstream_train.wandb.log = original_log
        if original_save is not None:
            upstream_train._checkpoints.save_state = original_save
        if journal is not None:
            journal.close()
    final_step = int(configured.num_train_steps) - 1
    final_step_dir = checkpoint_root / str(final_step)
    if not final_step_dir.is_dir():
        raise OpenPIPipelineError(
            f"upstream trainer returned without final checkpoint step {final_step}"
        )
    return checkpoint_root, resuming, None if journal is None else journal.path


def _set_rerun_step(rr: object, recording: object, step: int) -> None:
    if hasattr(rr, "set_time_sequence"):
        rr.set_time_sequence(RERUN_TIMELINE, step, recording=recording)
    else:
        rr.set_time(RERUN_TIMELINE, sequence=step, recording=recording)


def _rerun_executable() -> str:
    sibling = Path(sys.executable).with_name("rerun")
    if sibling.is_file():
        return str(sibling)
    value = shutil.which("rerun")
    if value:
        return value
    raise OpenPIPipelineError("rerun CLI is unavailable for RRD verification")


def _inspect_training_rrd(
    path: Path, *, run_id: str, source_telemetry_sha256: str
) -> dict[str, object]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise OpenPIPipelineError("Rerun recording is absent or empty")
    executable = _rerun_executable()
    verified = subprocess.run(
        [executable, "rrd", "verify", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if verified.returncode:
        raise OpenPIPipelineError(
            f"Rerun could not verify recording: {verified.stderr[-1000:]}"
        )
    printed = subprocess.run(
        [executable, "rrd", "print", "-vv", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if printed.returncode:
        raise OpenPIPipelineError(
            f"Rerun could not inspect recording: {printed.stderr[-1000:]}"
        )
    provenance = subprocess.run(
        [
            executable,
            "rrd",
            "print",
            "-vvv",
            "--entity",
            "provenance/run",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if provenance.returncode:
        raise OpenPIPipelineError(
            f"Rerun could not inspect recording provenance: {provenance.stderr[-1000:]}"
        )
    decoded = (
        f"{printed.stdout}\n{printed.stderr}\n"
        f"{provenance.stdout}\n{provenance.stderr}"
    )
    token = source_telemetry_sha256.encode("ascii")
    digest = hashlib.sha256()
    found_source = False
    overlap = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            searchable = overlap + chunk
            found_source = found_source or token in searchable
            overlap = searchable[-(len(token) - 1) :]
    if not found_source:
        raise OpenPIPipelineError(
            "RRD provenance does not identify its source telemetry bytes"
        )
    required = [
        RERUN_APPLICATION_ID,
        run_id,
        RERUN_TIMELINE,
        *REQUIRED_RRD_ENTITIES,
    ]
    missing = [value for value in required if value not in decoded]
    if missing:
        raise OpenPIPipelineError(
            "decoded RRD is missing required identity, timeline, or entities: "
            + ", ".join(missing)
        )
    return {
        "parseable": True,
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
        "application_id": RERUN_APPLICATION_ID,
        "recording_id": run_id,
        "timelines": [RERUN_TIMELINE],
        "entities": [
            entity for entity in REQUIRED_RRD_ENTITIES if entity in decoded
        ],
        "source_telemetry_sha256": source_telemetry_sha256,
    }


def _build_training_rrd(
    journal_path: Path,
    output_path: Path,
    *,
    run_id: str,
    config: object,
    prepared: Mapping[str, object],
    runtime_image: str,
    hardware: Mapping[str, object],
    topology: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    import rerun as rr
    import rerun.blueprint as rrb

    records = _load_telemetry_records(journal_path, run_id=run_id)
    source_telemetry_sha256 = hashlib.sha256(journal_path.read_bytes()).hexdigest()
    metric_records = {
        int(record["optimizer_step"]): record
        for record in records
        if record.get("record_type") == "metrics"
    }
    expected_steps = list(
        range(0, int(config.num_train_steps), int(config.log_interval))
    )
    if sorted(metric_records) != expected_steps:
        raise OpenPIPipelineError(
            "telemetry journal does not cover every upstream logging step"
        )
    checkpoint_events = {
        (int(record["optimizer_step"]), str(record.get("event")))
        for record in records
        if record.get("record_type") == "checkpoint"
    }
    final_step = int(config.num_train_steps) - 1
    expected_requested = {
        *range(
            int(config.save_interval),
            int(config.num_train_steps),
            int(config.save_interval),
        ),
        final_step,
    }
    missing_requested = sorted(
        step
        for step in expected_requested
        if (step, "save_requested") not in checkpoint_events
    )
    if missing_requested:
        raise OpenPIPipelineError(
            "telemetry lacks configured checkpoint save requests"
        )
    for event in ("save_requested", "materialized"):
        if (final_step, event) not in checkpoint_events:
            raise OpenPIPipelineError(
                f"telemetry lacks final checkpoint {event} event"
            )
    if sum(bool(record.get("interval")) for record in metric_records.values()) < 1:
        raise OpenPIPipelineError("telemetry lacks factual interval throughput")

    blueprint = rrb.Blueprint(
        rrb.Vertical(
            rrb.TimeSeriesView(origin="metrics", name="Loss and learning rate"),
            rrb.TimeSeriesView(
                origin="health", name="Gradient and distributed health"
            ),
            rrb.TimeSeriesView(
                origin="throughput", name="Interval training throughput"
            ),
            rrb.TimeSeriesView(origin="checkpoint", name="Checkpoint events"),
            rrb.TextDocumentView(origin="provenance", name="Run provenance"),
        ),
        rrb.TimePanel(state=rrb.PanelState.Expanded, timeline=RERUN_TIMELINE),
        auto_layout=False,
    )
    recording = rr.RecordingStream(RERUN_APPLICATION_ID, recording_id=run_id)
    rr.save(output_path, default_blueprint=blueprint, recording=recording)
    dataset = prepared.get("dataset") or {}
    normalization = prepared.get("normalization") or {}
    if not isinstance(dataset, Mapping) or not isinstance(normalization, Mapping):
        raise OpenPIPipelineError("preparation lineage is malformed")
    dataset_sha256 = str(dataset.get("listing_sha256", ""))
    normalization_sha256 = str(normalization.get("sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", dataset_sha256) or not re.fullmatch(
        r"[0-9a-f]{64}", normalization_sha256
    ):
        raise OpenPIPipelineError("preparation lineage lacks SHA-256 identities")
    provenance = (
        "# pi0.5 full-DROID fine-tuning\n\n"
        f"- run id: `{run_id}`\n"
        f"- producer: `npa.workbench.openpi.full_droid_finetune`\n"
        f"- upstream source ref: `{SOURCE_REF}`\n"
        f"- recipe: `{CONFIG_NAME}`\n"
        f"- optimizer steps: {int(config.num_train_steps)}\n"
        f"- global batch size: {int(config.batch_size)}\n"
        f"- dataset: `DROID 1.0.1`\n"
        f"- dataset listing sha256: `{dataset_sha256}`\n"
        f"- normalization sha256: `{normalization_sha256}`\n"
        f"- runtime image digest: `{_runtime_image_digest(runtime_image)}`\n"
        f"- source telemetry sha256: `{source_telemetry_sha256}`\n"
        "- learning rate: configured optimizer schedule value evaluated at the "
        "upstream optimizer step.\n"
        "- held-out/before-after policy trajectory: not produced by this "
        "offline training run; no stock or fabricated trajectory is included."
    )
    rr.log(
        "provenance/run",
        rr.TextDocument(provenance),
        static=True,
        recording=recording,
    )
    for step in expected_steps:
        record = metric_records[step]
        metrics = record.get("metrics") or {}
        health = record.get("health") or {}
        if not isinstance(metrics, Mapping) or not isinstance(health, Mapping):
            raise OpenPIPipelineError("telemetry metric payload is malformed")
        _set_rerun_step(rr, recording, step)
        rr.log("metrics/loss", rr.Scalars(float(metrics["loss"])), recording=recording)
        rr.log(
            "metrics/learning_rate",
            rr.Scalars(float(metrics["learning_rate"])),
            recording=recording,
        )
        rr.log(
            "health/gradient_norm",
            rr.Scalars(float(metrics["grad_norm"])),
            recording=recording,
        )
        rr.log(
            "health/param_norm",
            rr.Scalars(float(metrics["param_norm"])),
            recording=recording,
        )
        rr.log(
            "health/gradient_to_parameter_ratio",
            rr.Scalars(float(health["gradient_to_parameter_ratio"])),
            recording=recording,
        )
        rr.log(
            "health/nonfinite",
            rr.Scalars(0.0 if health.get("all_finite") is True else 1.0),
            recording=recording,
        )
        interval = record.get("interval")
        if isinstance(interval, Mapping):
            rr.log(
                "timing/interval_seconds",
                rr.Scalars(float(interval["seconds"])),
                recording=recording,
            )
            rr.log(
                "throughput/optimizer_steps_per_second",
                rr.Scalars(float(interval["optimizer_steps_per_second"])),
                recording=recording,
            )
            rr.log(
                "throughput/global_samples_per_second",
                rr.Scalars(float(interval["global_samples_per_second"])),
                recording=recording,
            )
    for step, event in sorted(checkpoint_events):
        _set_rerun_step(rr, recording, step)
        rr.log(f"checkpoint/{event}", rr.Scalars(1.0), recording=recording)
    distributed = {
        "health/distributed/process_count": int(hardware["process_count"]),
        "health/distributed/global_devices": int(hardware["global_gpu_count"]),
        "health/distributed/local_devices_per_process": int(
            hardware["local_devices_per_process"]
        ),
        "health/distributed/distinct_nodes": len(topology),
        "health/device/sm120_ranks": sum(
            "cc=12.0" in str(record.get("sm120_probe", "")) for record in topology
        ),
    }
    for step in (expected_steps[0], expected_steps[-1]):
        _set_rerun_step(rr, recording, step)
        for entity, value in distributed.items():
            rr.log(entity, rr.Scalars(float(value)), recording=recording)
    try:
        recording.flush()
    finally:
        recording.disconnect()
    return _inspect_training_rrd(
        output_path,
        run_id=run_id,
        source_telemetry_sha256=source_telemetry_sha256,
    )


def _runtime_image_digest(runtime_image: str) -> str:
    match = re.search(r"@(?P<digest>sha256:[0-9a-f]{64})$", runtime_image)
    if match is None:
        raise OpenPIPipelineError("runtime image must be pinned by SHA-256 digest")
    return match.group("digest")


def _write_once_or_verify(uri: str, payload: bytes, *, content_type: str) -> None:
    if _uri_exists(uri):
        if _read_bytes_uri(uri) != payload:
            raise OpenPIPipelineError("immutable artifact differs from this run")
        return
    try:
        _write_bytes_uri(uri, payload, content_type=content_type)
    except Exception:
        # S3 writes use If-None-Match. A racing retry is valid only when it
        # published the byte-identical artifact for this run.
        if not _uri_exists(uri) or _read_bytes_uri(uri) != payload:
            raise
    if _read_bytes_uri(uri) != payload:
        raise OpenPIPipelineError("artifact read-after-write verification failed")


def _publish_training_rrd(
    journal_path: Path,
    *,
    telemetry_uri: str,
    rrd_uri: str,
    run_id: str,
    config: object,
    prepared: Mapping[str, object],
    runtime_image: str,
    hardware: Mapping[str, object],
    topology: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    journal_payload = journal_path.read_bytes()
    _write_once_or_verify(
        telemetry_uri,
        journal_payload,
        content_type="application/x-ndjson",
    )
    with tempfile.TemporaryDirectory(prefix="npa-openpi-rerun-") as temporary:
        local = Path(temporary) / "full-droid-finetune.rrd"
        inspection = _build_training_rrd(
            journal_path,
            local,
            run_id=run_id,
            config=config,
            prepared=prepared,
            runtime_image=runtime_image,
            hardware=hardware,
            topology=topology,
        )
        _write_once_or_verify(
            rrd_uri, local.read_bytes(), content_type=RERUN_SCHEMA
        )
        readback = _read_bytes_uri(rrd_uri)
        readback_path = Path(temporary) / "readback.rrd"
        readback_path.write_bytes(readback)
        readback_inspection = _inspect_training_rrd(
            readback_path,
            run_id=run_id,
            source_telemetry_sha256=hashlib.sha256(journal_payload).hexdigest(),
        )
    return {
        "uri": rrd_uri,
        "schema": RERUN_SCHEMA,
        "inspection": readback_inspection,
        "producer_inspection": inspection,
        "source_telemetry": {
            "uri": telemetry_uri,
            "schema": TELEMETRY_SCHEMA,
            "bytes": len(journal_payload),
            "sha256": hashlib.sha256(journal_payload).hexdigest(),
        },
    }


def _local_hardware_evidence() -> dict[str, object]:
    import jax
    from jax.extend import backend as jax_backend

    local_devices = jax.local_devices()
    global_devices = jax.devices()
    if (
        len(local_devices) != EXPECTED_LOCAL_DEVICES
        or len(global_devices) != EXPECTED_DEVICES
    ):
        raise OpenPIPipelineError(
            "JAX device counts changed after topology initialization"
        )
    global_kinds = [str(device.device_kind) for device in global_devices]
    if any("RTX PRO 6000" not in kind.upper() for kind in global_kinds):
        raise OpenPIPipelineError(
            f"expected RTX PRO 6000 devices, got {global_kinds!r}"
        )
    nvidia_smi = (
        subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,compute_cap,driver_version,memory.total",
                "--format=csv,noheader",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .splitlines()
    )
    if len(nvidia_smi) != EXPECTED_LOCAL_DEVICES:
        raise OpenPIPipelineError("nvidia-smi must expose exactly one GPU per process")
    if "RTX PRO 6000" not in nvidia_smi[0].upper() or not re.search(
        r"(?:^|,)\s*12\.0\s*(?:,|$)", nvidia_smi[0]
    ):
        raise OpenPIPipelineError(
            f"expected an RTX PRO 6000 SM120 GPU, got {nvidia_smi!r}"
        )
    return {
        "global_gpu_count": len(global_devices),
        "local_gpu_count": len(local_devices),
        "global_device_kinds": global_kinds,
        "local_nvidia_smi": nvidia_smi,
        "jax": jax.__version__,
        "jaxlib": importlib.metadata.version("jaxlib"),
        "xla_platform_version": str(jax_backend.get_backend().platform_version),
    }


def _write_rank_evidence(
    work_root: Path, rank: int, hardware: dict[str, object], probe: str
) -> dict[str, object]:
    import jax

    local_devices = jax.local_devices()
    evidence: dict[str, object] = {
        "rank": rank,
        "hostname_sha256": hashlib.sha256(socket.gethostname().encode()).hexdigest(),
        "local_device_count": len(local_devices),
        "global_device_count": jax.device_count(),
        "device_kinds": sorted({str(device.device_kind) for device in local_devices}),
        "local_nvidia_smi": hardware["local_nvidia_smi"],
        "sm120_probe": probe,
    }
    root = work_root / "topology"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"rank-{rank}.json").write_text(
        json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8"
    )
    return evidence


def _read_topology(work_root: Path) -> list[dict[str, object]]:
    records = [
        json.loads(
            (work_root / "topology" / f"rank-{rank}.json").read_text(encoding="utf-8")
        )
        for rank in range(EXPECTED_PROCESSES)
    ]
    if [record.get("rank") for record in records] != list(range(EXPECTED_PROCESSES)):
        raise OpenPIPipelineError("rank evidence is incomplete")
    if len({record.get("hostname_sha256") for record in records}) != EXPECTED_PROCESSES:
        raise OpenPIPipelineError("rank evidence does not prove eight distinct nodes")
    if any(record.get("local_device_count") != 1 for record in records):
        raise OpenPIPipelineError("rank evidence does not prove one GPU per node")
    if any(
        "RTX PRO 6000"
        not in " ".join(str(value) for value in record.get("device_kinds", [])).upper()
        for record in records
    ):
        raise OpenPIPipelineError("rank evidence contains a non-RTX-PRO-6000 device")
    if any("cc=12.0" not in str(record.get("sm120_probe", "")) for record in records):
        raise OpenPIPipelineError("rank evidence does not prove SM120 on every node")
    return records


def _fine_tune(args: argparse.Namespace) -> int:
    _require_terms()
    repo_root = Path(args.repo_root)
    build = _validate_source(repo_root, args.runtime_image)
    prepared = _read_json_uri(args.prepare_uri)
    if (
        prepared.get("schema") != "npa.workbench.openpi.pi05-full-droid-prepare.v1"
        or prepared.get("status") != "passed"
    ):
        raise OpenPIPipelineError(
            "full-DROID preparation artifact is absent or invalid"
        )

    rank, node_ips = _multihost_environment()
    multihost_utils = _initialize_multihost(rank, node_ips)
    _install_distributed_rlds_adapter(rank)

    import jax
    from openpi.training import sharding

    hardware = _local_hardware_evidence()
    mesh = sharding.make_mesh(EXPECTED_FSDP_DEVICES)
    if tuple(mesh.devices.shape) != (1, EXPECTED_FSDP_DEVICES):
        raise OpenPIPipelineError(f"unexpected FSDP mesh shape {mesh.devices.shape}")
    probe = subprocess.run(
        ["/usr/local/bin/npa-openpi-sm120-probe"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    work_root = Path(args.work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    config = _configured_upstream(Path(args.data_root), work_root, args.experiment)
    checkpoint_root, resumed, telemetry_path = _run_training(
        config, repo_root, rank=rank, run_id=args.run_id
    )
    _write_rank_evidence(work_root, rank, hardware, probe)
    multihost_utils.sync_global_devices("npa-openpi-training-finished")

    if rank != 0:
        multihost_utils.sync_global_devices("npa-openpi-artifacts-published")
        print(
            json.dumps({"status": "passed", "rank": rank}, sort_keys=True), flush=True
        )
        return 0

    topology = _read_topology(work_root)
    checkpoint = _upload_checkpoint(checkpoint_root, args.checkpoint_uri)
    hardware_summary = {
        **hardware,
        "process_count": jax.process_count(),
        "local_devices_per_process": EXPECTED_LOCAL_DEVICES,
        "distinct_nodes": len(topology),
    }
    if telemetry_path is None:
        raise OpenPIPipelineError("rank zero telemetry journal is absent")
    rerun = _publish_training_rrd(
        telemetry_path,
        telemetry_uri=args.telemetry_uri,
        rrd_uri=args.rrd_uri,
        run_id=args.run_id,
        config=config,
        prepared=prepared,
        runtime_image=args.runtime_image,
        hardware=hardware_summary,
        topology=topology,
    )
    result: dict[str, object] = {
        "schema": "npa.workbench.openpi.pi05-full-droid-finetune.v1",
        "status": "passed",
        "source": {
            "repository": "https://github.com/Physical-Intelligence/openpi",
            "ref": SOURCE_REF,
            "license": "Apache-2.0",
            **build,
        },
        "runtime_image": args.runtime_image,
        "redistribution": _redistribution_evidence(trained_checkpoint=True),
        "recipe": {
            "config_name": CONFIG_NAME,
            "dataset": prepared["dataset"],
            "normalization": prepared["normalization"],
            "global_batch_size": EXPECTED_BATCH_SIZE,
            "batch_per_process": EXPECTED_BATCH_SIZE // EXPECTED_PROCESSES,
            "optimizer_steps": EXPECTED_STEPS,
            "fsdp_devices": EXPECTED_FSDP_DEVICES,
            "mesh_shape": [1, EXPECTED_FSDP_DEVICES],
            "upstream_entrypoint": "scripts/train.py:main",
            "upstream_recipe_hyperparameters_unmodified": True,
            "distributed_rlds_adapter": "pre_shuffle_process_shard_and_local_batch",
            "distributed_shuffle_seed": "upstream_seed_plus_process_index",
            "checkpoint_coordination": "orbax_checkpoint_manager_primary_host_and_global_barriers",
        },
        "checkpoint": {
            "uri": args.checkpoint_uri.rstrip("/") + "/",
            "manifest_uri": args.checkpoint_uri.rstrip("/") + "/manifest.json",
            "content_manifest_sha256": checkpoint["content_manifest_sha256"],
            "file_count": checkpoint["file_count"],
            "total_size_bytes": checkpoint["total_size_bytes"],
            "final_step": EXPECTED_STEPS,
            "upstream_checkpoint_directory": EXPECTED_STEPS - 1,
            "resumed_from_durable_checkpoint": resumed,
        },
        "hardware": {
            **hardware_summary,
            "rank_evidence": topology,
            "sm120_probe": probe,
        },
        "rerun": rerun,
        "terms": {"forwarded": True, "persisted": False},
        "timings_seconds": {"total": round(time.perf_counter() - started, 3)},
        "limitations": ["offline_training_does_not_prove_physical_robot_success"],
    }
    _write_json_uri(args.output_uri, result)
    multihost_utils.sync_global_devices("npa-openpi-artifacts-published")
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--runtime-image", required=True)
    common.add_argument("--repo-root", default="/opt/byof")
    common.add_argument("--work-root", default="/workspace/openpi-full-droid")
    common.add_argument("--data-root", default="/workspace/openpi-full-droid/dataset")
    common.add_argument("--experiment", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", parents=[common])
    prepare.add_argument("--output-uri", required=True)
    prepare.add_argument("--gsutil", default="/opt/gsutil-venv/bin/gsutil")
    prepare.set_defaults(func=_prepare)

    train = subparsers.add_parser("train", parents=[common])
    train.add_argument("--prepare-uri", required=True)
    train.add_argument("--output-uri", required=True)
    train.add_argument("--checkpoint-uri", required=True)
    train.add_argument("--telemetry-uri", required=True)
    train.add_argument("--rrd-uri", required=True)
    train.add_argument("--run-id", required=True)
    train.set_defaults(func=_fine_tune)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
