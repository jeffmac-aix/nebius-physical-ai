from __future__ import annotations

from pathlib import Path
import hashlib
import subprocess
import sys
import types
from types import SimpleNamespace

import pytest
import yaml

from npa.orchestration.npa_workflow.catalog import TOOL_CATALOG
from npa.workflows.byof import openpi_full_droid as full_droid


REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = (
    REPO_ROOT
    / "npa"
    / "workflows"
    / "workbench"
    / "npa-workflows"
    / "openpi-pi05-full-droid-finetune.yaml"
)


def test_full_droid_recipe_matches_pinned_upstream_contract() -> None:
    assert full_droid.SOURCE_REF == "15a9616a00943ada6c20a0f158e3adb39df2ccac"
    assert full_droid.CONFIG_NAME == "pi05_full_droid_finetune"
    assert full_droid.DATASET_URI == "gs://gresearch/robotics/droid/1.0.1"
    assert full_droid.EXPECTED_STEPS == 100_000
    assert full_droid.EXPECTED_BATCH_SIZE == 256
    assert full_droid.EXPECTED_DEVICES == full_droid.EXPECTED_FSDP_DEVICES == 8
    assert full_droid.EXPECTED_BATCH_SIZE % full_droid.EXPECTED_DEVICES == 0
    assert full_droid.NORM_MAX_FRAMES == 10_000_000


def test_full_droid_spec_is_exactly_eight_one_gpu_nodes() -> None:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    config = spec["config"]
    profile = spec["resources"]["rtxpro8"]
    assert config["gpu_type"] == "RTXPRO6000"
    assert config["gpu_count"] == "1"
    assert config["gpu_num_nodes"] == "8"
    assert config["multi_host_enabled"] == "true"
    assert profile["accelerators"] == "{{config.gpu_type}}:{{config.gpu_count}}"
    assert profile["num_nodes"] == "{{config.gpu_num_nodes}}"
    assert "persistentVolumeClaim" in str(profile)
    prepare = spec["states"]["prepare_full_droid"]
    assert prepare["toolRef"] == "workbench.openpi.full_droid_prepare"
    assert prepare["next"] == "full_droid_finetune"
    state = spec["states"]["full_droid_finetune"]
    assert state["toolRef"] == "workbench.openpi.full_droid_finetune"
    assert state["terminal"] is True
    outputs = {item["uri"]: item["schema"] for item in state["outputs"]}
    assert outputs["{{config.rrd_uri}}"] == "application/vnd.rerun.rrd"
    assert outputs["{{config.telemetry_uri}}"] == full_droid.TELEMETRY_SCHEMA
    assert spec["config"]["rrd_uri"].endswith(
        "reports/full-droid-finetune.rrd"
    )
    assert "{{run.id}}" in spec["config"]["prefix"]


def test_full_droid_toolref_has_no_tunable_recipe_shortcuts() -> None:
    entry = TOOL_CATALOG["workbench.openpi.full_droid_finetune"]
    argv = entry.argv_template
    assert argv[:4] == [
        "/opt/venv/bin/python",
        "-m",
        "npa.workflows.byof.openpi_full_droid",
        "train",
    ]
    assert "--train-steps" not in argv
    assert "--batch-size" not in argv
    assert "--fsdp-devices" not in argv
    assert "--checkpoint-uri" in argv
    assert "--telemetry-uri" in argv
    assert "--rrd-uri" in argv
    assert "--run-id" in argv
    assert entry.multi_node_mode == "sharded"
    assert entry.shard_activation_config == "multi_host_enabled"
    assert entry.shard_output_config == "trained_checkpoint_uri"


def test_prepare_toolref_has_no_gpu_training_flags() -> None:
    argv = TOOL_CATALOG["workbench.openpi.full_droid_prepare"].argv_template
    assert argv[:4] == [
        "/opt/venv/bin/python",
        "-m",
        "npa.workflows.byof.openpi_full_droid",
        "prepare",
    ]
    assert "--checkpoint-uri" not in argv
    assert "--output-uri" in argv


def test_distributed_rlds_adapter_shards_before_shuffle_and_uses_local_batch(
    monkeypatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeDataset:
        def shard(self, process_count, rank):
            calls["shard"] = (process_count, rank)
            return self

    class FakeDLataset:
        @staticmethod
        def sample_from_datasets(*args, **kwargs):
            calls["sample"] = (args, kwargs)
            return FakeDataset()

    class FakeRLDSDataLoader:
        pass

    def original_create(data_config, action_horizon, batch_size, *, shuffle=False):
        calls["create"] = (data_config, action_horizon, batch_size, shuffle)
        return FakeDataset()

    fake_loader = types.ModuleType("openpi.training.data_loader")
    fake_loader.create_rlds_dataset = original_create
    fake_loader.RLDSDataLoader = FakeRLDSDataLoader
    fake_training = types.ModuleType("openpi.training")
    fake_training.data_loader = fake_loader
    fake_openpi = types.ModuleType("openpi")
    fake_openpi.training = fake_training
    fake_tensorflow = types.ModuleType("tensorflow")
    fake_tensorflow.random = types.SimpleNamespace(
        set_seed=lambda value: calls.__setitem__("seed", value)
    )
    fake_jax = types.ModuleType("jax")
    fake_jax.sharding = types.SimpleNamespace()
    fake_dlimp = types.ModuleType("dlimp")
    fake_dlimp.DLataset = FakeDLataset
    for name, module in {
        "dlimp": fake_dlimp,
        "jax": fake_jax,
        "tensorflow": fake_tensorflow,
        "openpi": fake_openpi,
        "openpi.training": fake_training,
        "openpi.training.data_loader": fake_loader,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    full_droid._install_distributed_rlds_adapter(rank=3)
    fake_dlimp.DLataset.sample_from_datasets([FakeDataset()], weights=[1.0])
    fake_loader.create_rlds_dataset("data", 16, 256, shuffle=True)

    assert calls["seed"] == 45
    assert calls["shard"] == (8, 3)
    assert calls["create"] == ("data", 16, 32, True)


def test_remote_inventory_is_content_addressed(monkeypatch, tmp_path: Path) -> None:
    listing = (
        "       12  2026-01-01T00:00:00Z  gs://gresearch/robotics/droid/1.0.1/a\n"
        "       34  2026-01-01T00:00:01Z  gs://gresearch/robotics/droid/1.0.1/b\n"
        "TOTAL: 2 objects, 46 bytes (46 B)\n"
    )

    def fake_run(command, *, cwd=None, stdout=None):
        del command, cwd
        stdout.write(listing)

    monkeypatch.setattr(full_droid, "_run", fake_run)
    result = full_droid._remote_inventory("gsutil", tmp_path / "listing.txt")
    assert result["object_count"] == 2
    assert result["total_size_bytes"] == 46
    assert len(str(result["listing_sha256"])) == 64


def _telemetry_config() -> SimpleNamespace:
    return SimpleNamespace(
        num_train_steps=2,
        batch_size=256,
        log_interval=1,
        save_interval=2,
    )


def test_telemetry_journal_is_durable_deduplicated_and_run_scoped(
    tmp_path: Path,
) -> None:
    path = tmp_path / "telemetry.jsonl"
    config = _telemetry_config()
    journal = full_droid._TrainingTelemetryJournal(
        path, run_id="rrd-unit-run", config=config
    )
    journal.record_metrics(
        step=0,
        values={"loss": 1.2, "grad_norm": 0.4, "param_norm": 20.0},
        learning_rate=1e-6,
    )
    journal.record_metrics(
        step=1,
        values={"loss": 1.0, "grad_norm": 0.3, "param_norm": 20.1},
        learning_rate=2e-6,
    )
    journal.record_metrics(
        step=1,
        values={"loss": 99.0, "grad_norm": 99.0, "param_norm": 99.0},
        learning_rate=99.0,
    )
    journal.record_checkpoint(step=1, event="save_requested")
    journal.record_checkpoint(step=1, event="materialized")
    journal.close()

    records = full_droid._load_telemetry_records(path, run_id="rrd-unit-run")
    metrics = [record for record in records if record["record_type"] == "metrics"]
    assert [record["optimizer_step"] for record in metrics] == [0, 1]
    assert metrics[1]["metrics"]["loss"] == 1.0
    assert metrics[1]["interval"]["optimizer_steps"] == 1
    assert metrics[1]["interval"]["global_samples_per_second"] > 0
    assert "hostname" not in path.read_text(encoding="utf-8")
    assert "s3://" not in path.read_text(encoding="utf-8")


def test_telemetry_resume_deduplicates_without_inventing_cross_segment_timing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "telemetry.jsonl"
    config = SimpleNamespace(
        num_train_steps=3,
        batch_size=256,
        log_interval=1,
        save_interval=3,
    )
    first = full_droid._TrainingTelemetryJournal(
        path, run_id="rrd-resume", config=config
    )
    first.record_metrics(
        step=0,
        values={"loss": 1.2, "grad_norm": 0.4, "param_norm": 20.0},
        learning_rate=1e-6,
    )
    first.close()

    resumed = full_droid._TrainingTelemetryJournal(
        path, run_id="rrd-resume", config=config
    )
    resumed.record_metrics(
        step=0,
        values={"loss": 99.0, "grad_norm": 99.0, "param_norm": 99.0},
        learning_rate=99.0,
    )
    resumed.record_metrics(
        step=1,
        values={"loss": 1.1, "grad_norm": 0.3, "param_norm": 20.0},
        learning_rate=2e-6,
    )
    resumed.record_metrics(
        step=2,
        values={"loss": 1.0, "grad_norm": 0.2, "param_norm": 20.0},
        learning_rate=3e-6,
    )
    resumed.close()

    metrics = [
        record
        for record in full_droid._load_telemetry_records(
            path, run_id="rrd-resume"
        )
        if record["record_type"] == "metrics"
    ]
    assert [record["optimizer_step"] for record in metrics] == [0, 1, 2]
    assert [record["segment"] for record in metrics] == [1, 2, 2]
    assert metrics[1]["interval"] is None
    assert metrics[2]["interval"]["optimizer_steps"] == 1


def test_telemetry_loader_rejects_non_integer_optimizer_step(
    tmp_path: Path,
) -> None:
    path = tmp_path / "telemetry.jsonl"
    path.write_text(
        '{"schema":"'
        + full_droid.TELEMETRY_SCHEMA
        + '","run_id":"bad-step","segment":1,'
        '"record_type":"metrics","optimizer_step":"1"}\n',
        encoding="utf-8",
    )
    with pytest.raises(
        full_droid.OpenPIPipelineError, match="invalid step or segment"
    ):
        full_droid._load_telemetry_records(path, run_id="bad-step")


def test_real_rrd_contains_run_identity_timeline_and_review_entities(
    tmp_path: Path,
) -> None:
    run_id = "rrd-unit-run"
    config = _telemetry_config()
    journal_path = tmp_path / "telemetry.jsonl"
    journal = full_droid._TrainingTelemetryJournal(
        journal_path, run_id=run_id, config=config
    )
    for step, loss in enumerate((1.2, 1.0)):
        journal.record_metrics(
            step=step,
            values={"loss": loss, "grad_norm": 0.4, "param_norm": 20.0},
            learning_rate=(step + 1) * 1e-6,
        )
    journal.record_checkpoint(step=1, event="save_requested")
    journal.record_checkpoint(step=1, event="materialized")
    journal.close()

    output = tmp_path / "training.rrd"
    inspection = full_droid._build_training_rrd(
        journal_path,
        output,
        run_id=run_id,
        config=config,
        prepared={
            "dataset": {"listing_sha256": "a" * 64},
            "normalization": {"sha256": "b" * 64},
        },
        runtime_image="ghcr.io/example/openpi@sha256:" + "c" * 64,
        hardware={
            "process_count": 8,
            "global_gpu_count": 8,
            "local_devices_per_process": 1,
        },
        topology=[{"sm120_probe": "devices=1 cc=12.0"} for _ in range(8)],
    )

    assert output.stat().st_size > 0
    assert inspection["parseable"] is True
    assert inspection["recording_id"] == run_id
    assert inspection["timelines"] == ["optimizer_step"]
    assert set(full_droid.REQUIRED_RRD_ENTITIES) <= set(inspection["entities"])
    assert inspection["source_telemetry_sha256"] == hashlib.sha256(
        journal_path.read_bytes()
    ).hexdigest()
    decoded_loss = subprocess.run(
        [
            full_droid._rerun_executable(),
            "rrd",
            "print",
            "-vvv",
            "--entity",
            "metrics/loss",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "optimizer_step" in decoded_loss
    assert "[1.2]" in decoded_loss
    assert "[1.0]" in decoded_loss


def test_runtime_image_provenance_requires_only_a_digest() -> None:
    digest = "sha256:" + "c" * 64
    assert (
        full_droid._runtime_image_digest("ghcr.io/example/openpi@" + digest)
        == digest
    )
    with pytest.raises(
        full_droid.OpenPIPipelineError, match="must be pinned by SHA-256"
    ):
        full_droid._runtime_image_digest("private.registry.invalid/openpi:latest")


def test_write_once_is_idempotent_and_rejects_conflicting_bytes(
    tmp_path: Path,
) -> None:
    target = str(tmp_path / "artifact.rrd")
    full_droid._write_once_or_verify(
        target, b"first", content_type=full_droid.RERUN_SCHEMA
    )
    full_droid._write_once_or_verify(
        target, b"first", content_type=full_droid.RERUN_SCHEMA
    )
    with pytest.raises(
        full_droid.OpenPIPipelineError,
        match="immutable artifact differs from this run",
    ):
        full_droid._write_once_or_verify(
            target, b"different", content_type=full_droid.RERUN_SCHEMA
        )


def test_rrd_refuses_incomplete_actual_metric_history(tmp_path: Path) -> None:
    run_id = "rrd-incomplete"
    config = _telemetry_config()
    journal_path = tmp_path / "telemetry.jsonl"
    journal = full_droid._TrainingTelemetryJournal(
        journal_path, run_id=run_id, config=config
    )
    journal.record_metrics(
        step=0,
        values={"loss": 1.0, "grad_norm": 0.4, "param_norm": 20.0},
        learning_rate=1e-6,
    )
    journal.record_checkpoint(step=1, event="save_requested")
    journal.record_checkpoint(step=1, event="materialized")
    journal.close()

    with pytest.raises(
        full_droid.OpenPIPipelineError,
        match="does not cover every upstream logging step",
    ):
        full_droid._build_training_rrd(
            journal_path,
            tmp_path / "incomplete.rrd",
            run_id=run_id,
            config=config,
            prepared={"dataset": {}, "normalization": {}},
            runtime_image="ghcr.io/example/openpi@sha256:" + "d" * 64,
            hardware={
                "process_count": 8,
                "global_gpu_count": 8,
                "local_devices_per_process": 1,
            },
            topology=[{"sm120_probe": "devices=1 cc=12.0"} for _ in range(8)],
        )
