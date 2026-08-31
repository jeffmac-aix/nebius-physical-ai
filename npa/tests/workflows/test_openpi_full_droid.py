from __future__ import annotations

from pathlib import Path
import sys
import types

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
