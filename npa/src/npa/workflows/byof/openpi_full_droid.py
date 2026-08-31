"""Prepare and run pinned full-DROID pi0.5 fine-tuning on eight GPU nodes."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.metadata
import json
import os
import re
import socket
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from npa.workflows.byof.openpi_pipeline import (
    OpenPIPipelineError,
    _load_upstream_train_module,
    _read_json_uri,
    _redistribution_evidence,
    _source_build_evidence,
    _upload_checkpoint,
    _validate_runtime_image,
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


def _run_training(config: object, repo_root: Path) -> tuple[Path, bool]:
    checkpoint_root = _checkpoint_root(config)
    resuming = checkpoint_root.is_dir() and any(checkpoint_root.iterdir())
    configured = dataclasses.replace(config, resume=resuming, overwrite=not resuming)
    upstream_train = _load_upstream_train_module(repo_root)
    upstream_train.main(configured)
    final_step_dir = checkpoint_root / str(EXPECTED_STEPS - 1)
    if not final_step_dir.is_dir():
        raise OpenPIPipelineError(
            f"upstream trainer returned without final checkpoint step {EXPECTED_STEPS - 1}"
        )
    return checkpoint_root, resuming


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
    checkpoint_root, resumed = _run_training(config, repo_root)
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
            **hardware,
            "process_count": jax.process_count(),
            "local_devices_per_process": EXPECTED_LOCAL_DEVICES,
            "distinct_nodes": len(topology),
            "rank_evidence": topology,
            "sm120_probe": probe,
        },
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
    train.set_defaults(func=_fine_tune)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
