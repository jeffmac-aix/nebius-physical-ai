"""Unit coverage for the living-lab 16-zone neural-reconstruction digital twin.

No S3, GPU, NGC, or HF needed: the join and zone model are pure logic, and the
fan-out spec is asserted for real-component / GPU-routing invariants.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from npa.orchestration.npa_workflow.catalog import TOOL_CATALOG
from npa.workflows import living_lab

SPEC_PATH = (
    Path(__file__).resolve().parents[2]
    / "workflows"
    / "workbench"
    / "npa-workflows"
    / "living-lab-nurec-fanout.yaml"
)


@pytest.fixture()
def local_storage(monkeypatch, tmp_path: Path):
    root = tmp_path / "s3"

    def _local(uri: str) -> Path:
        return root / uri[len("s3://") :] if uri.startswith("s3://") else Path(uri)

    def fake_download_json(uri: str):
        path = _local(uri)
        if not path.is_file():
            raise FileNotFoundError(uri)
        return json.loads(path.read_text(encoding="utf-8"))

    def fake_upload_file(local: Path, uri: str):
        dest = _local(uri)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(Path(local).read_bytes())
        return uri

    def fake_upload_bytes(payload: bytes, uri: str):
        dest = _local(uri)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        return uri

    def fake_download_path(uri: str, tmp: str):
        path = _local(uri)
        if path.is_dir():
            imgs = sorted(
                p for p in path.rglob("*") if p.suffix in (".png", ".jpg", ".jpeg")
            )
            if not imgs:
                raise FileNotFoundError(uri)
            return imgs[0]
        if not path.is_file():
            raise FileNotFoundError(uri)
        return path

    monkeypatch.setattr(living_lab, "_download_json", fake_download_json)
    monkeypatch.setattr(living_lab, "_upload_file", fake_upload_file)
    monkeypatch.setattr(living_lab, "_upload_bytes", fake_upload_bytes)
    monkeypatch.setattr(living_lab, "_download_path", fake_download_path)
    return root


def _write_zone(root: Path, zone_name: str, *, gpu: str = "RTX PRO 6000") -> None:
    path = (
        root
        / "bucket/living-lab/zones"
        / zone_name
        / living_lab.ZONE_MANIFEST_FILENAME
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "npa.living_lab.zone_manifest.v1",
                "zone_name": zone_name,
                "status": "ok",
                "gpu_name": gpu,
                "usdz_path": f"s3://bucket/living-lab/zones/{zone_name}/reconstruction/last.usdz",
                "reconstruction_uri": f"s3://bucket/living-lab/zones/{zone_name}/reconstruction/",
                "novel_views_uri": f"s3://bucket/living-lab/zones/{zone_name}/novel_views/",
                "metrics": {"test/psnr": 31.19, "test/ssim": 0.833},
            }
        ),
        encoding="utf-8",
    )
    # a real, openable preview frame for the panorama
    png = path.parent / "novel_views/frame.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    from PIL import Image

    Image.new("RGB", (32, 32), (int(120), int(80), int(40))).save(png)


def test_living_lab_zones_are_exactly_16_and_deterministic() -> None:
    zones = living_lab.living_lab_zones()
    assert len(zones) == 16
    names = [z["zone_name"] for z in zones]
    assert len(set(names)) == 16
    # every real scene x variant appears twice (view sectors a and b)
    from collections import Counter

    seq_counts = Counter(
        f"{z['scene']}-{z['variant']}" for z in zones
    )
    assert all(count == 2 for count in seq_counts.values())
    assert len(seq_counts) == 8
    # deterministic ordering
    assert living_lab.living_lab_zones() == zones


def test_all_zones_have_novel_non_zero_render_offsets() -> None:
    for zone in living_lab.living_lab_zones():
        trans = tuple(float(v) for v in str(zone["rig_translation_offset"]).split(","))
        rot = tuple(float(v) for v in str(zone["rig_rotation_offset"]).split(","))
        assert any(trans), zone["zone_name"]
        assert any(rot) or any(v != 0.0 for v in trans), zone["zone_name"]


def test_zone_uris_are_run_scoped() -> None:
    uris = living_lab.zone_uris(
        run_uri="s3://bucket/living-lab/run-1", zone_name="toro-auto-a"
    )
    assert uris["zone_uri"] == "s3://bucket/living-lab/run-1/zones/toro-auto-a/"
    assert uris["manifest_uri"].endswith(living_lab.ZONE_MANIFEST_FILENAME)
    assert uris["rrd_uri"].endswith("reports/sim2real.rrd")


def test_zone_names_defaults_to_16(local_storage) -> None:
    assert len(living_lab.zone_names()) == 16
    assert living_lab.zone_names("a,b") == ["a", "b"]


def test_join_merges_all_16_zones(local_storage) -> None:
    for zone in living_lab.living_lab_zones():
        _write_zone(local_storage, zone["zone_name"])

    report = living_lab.join_living_lab_zones(
        zones_uri="s3://bucket/living-lab/zones/",
        report_uri="s3://bucket/living-lab/reports/",
        panorama_uri="s3://bucket/living-lab/reports/panorama.png",
        run_id="run-1",
    )

    assert report["schema"] == living_lab.DIGITAL_TWIN_SCHEMA
    assert report["zone_count"] == 16
    assert report["joined_zones"] == 16
    assert report["missing_zones"] == []
    assert report["distinct_gpu_count"] == 1
    assert report["aggregate_metrics"]["test/ssim_mean"] == 0.833
    assert report["panorama"]["cells"] == 16
    assert report["panorama"]["panorama_uri"].endswith("panorama.png")
    written = json.loads(
        (
            local_storage / "bucket/living-lab/reports/digital_twin.json"
        ).read_text()
    )
    assert len(written["zones"]) == 16


def test_join_fails_when_a_zone_is_missing(local_storage) -> None:
    for zone in living_lab.living_lab_zones()[:15]:
        _write_zone(local_storage, zone["zone_name"])

    with pytest.raises(RuntimeError, match="1 of 16 zones missing"):
        living_lab.join_living_lab_zones(
            zones_uri="s3://bucket/living-lab/zones/",
            report_uri="s3://bucket/living-lab/reports/",
            panorama_uri="s3://bucket/living-lab/reports/panorama.png",
        )


def test_join_requires_real_gpu_and_usdz(local_storage) -> None:
    for zone in living_lab.living_lab_zones():
        _write_zone(local_storage, zone["zone_name"], gpu="")
    with pytest.raises(RuntimeError):
        living_lab.join_living_lab_zones(
            zones_uri="s3://bucket/living-lab/zones/",
            report_uri="s3://bucket/living-lab/reports/",
            panorama_uri="s3://bucket/living-lab/reports/panorama.png",
        )


def test_spec_has_16_gpu_shards_and_a_join() -> None:
    spec = living_lab.build_living_lab_workflow_spec()
    states = spec["states"]
    shard_names = [n for n in states if n.startswith("zone-")]
    assert len(shard_names) == 16
    group = states["living-lab-zones"]
    assert list(group["parallel"]) == shard_names
    assert group["maxConcurrency"] == "{{config.max_concurrency}}"
    assert group["next"] == "join"
    join = states["join"]
    assert join["needs"] == ["living-lab-zones"]
    assert join["terminal"] is True


def test_every_shard_is_real_nurec_work_on_rtx_gpu() -> None:
    spec = living_lab.build_living_lab_workflow_spec()
    gpu = spec["resources"]["gpu"]
    for name, state in spec["states"].items():
        if not name.startswith("zone-"):
            continue
        assert state["resources"] == "gpu"
        shell = state["run"]["shell"]
        # Every shard runs the full real single-pod NRE pipeline inside the
        # NRE container: check -> fetch -> reconstruct -> render -> visualize
        # -> finalize, exactly like the validated reference
        # (npa/src/npa/workbench/nurec/examples/nurec-reconstruct.yaml).
        assert "npa workbench nurec check" in shell
        assert "npa workbench nurec fetch" in shell
        assert "npa workbench nurec reconstruct" in shell
        assert "npa workbench nurec render" in shell
        assert "npa workbench nurec visualize" in shell
        assert "npa workbench nurec finalize" in shell
        # One GPU per shard: never a disguised single-GPU-unaware program.
        assert "--world-size 1" in shell
        # Novel-view rendering requires a real non-zero rig offset.
        assert "--rig-translation-offset" in shell and "--rig-rotation-offset" in shell

        # --- flag-level correctness (catches CLI-flag drift that validates
        # but crashes on real submit) -------------------------------------
        # fetch -> reconstruct handoff: same pod, so --ncore-json points at the
        # local meta-file the fetch stage unpacked (never --ncore-uri, which
        # expects an S3 *published sequence* that fetch --publish-sequence writes).
        assert "--ncore-json" in shell and "--ncore-uri" not in shell
        # reconstruct needs the derived rig pose group + reference camera.
        assert "--poses-component-group" in shell
        assert "--camera-id" in shell
        # render must target the trained .usdz artifact, not an S3 dir, and
        # must pass --camera-id (required by `nre render`).
        assert "--artifact-path" in shell
        assert shell.count("--camera-id") == 2  # reconstruct + render require it

        # The zone run root must expose input/, reconstruction/, novel_views/
        # for visualize, so it uses the zone prefix ""+ ZU, not a sub-dir.
        assert 'visualize --input-uri "${ZU}"' in shell

        # The NRE container ships no npa/ffmpeg/runtime deps: the shard must
        # install them (ffmpeg + nvidia-ncore + rerun-sdk + npa guard).
        assert "ffmpeg" in shell
        assert "nvidia-ncore" in shell
        assert "rerun-sdk" in shell
        assert "command -v npa" in shell

        # Manifest writer is a child python process: the shell vars it reads
        # must be exported.
        assert "export ZONE" in shell
        assert "export GPU_NAME" in shell
        assert "export USDZ" in shell
    assert gpu["accelerators"] == "RTXPRO-6000-BLACKWELL-SERVER-EDITION:1", (
        "shard must route to RTX PRO 6000"
    )


def test_no_stub_toolrefs_and_all_shells_are_real() -> None:
    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    for name, state in spec["states"].items():
        tool_ref = state.get("toolRef")
        if tool_ref:
            assert tool_ref in TOOL_CATALOG
            assert TOOL_CATALOG[tool_ref].stub is False, name
        run = state.get("run")
        if not run:
            continue
        command = str(run.get("shell", "")) or " ".join(
            str(item) for item in run.get("argv", [])
        )
        assert "npa workbench" in command or "living_lab" in command, (
            f"state '{name}' run is not a real command/module call"
        )


def test_committed_yaml_matches_generator() -> None:
    assert SPEC_PATH.read_text() == living_lab.living_lab_workflow_yaml()


def test_shard_shell_runs_full_pipeline_and_writes_manifest(tmp_path) -> None:
    """Execute a resolved zone shard shell end-to-end with stubbed tools.

    Guards the *operational* correctness the flag checks cannot: the shard must
    actually install + guard runtime deps, run every real nurec verb in order,
    export the shell vars a child python process reads, and publish a
    load-bearing zone_manifest.json — not crash on os.environ KeyError.
    """
    import os
    import subprocess
    import sys

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    boost = tmp_path / "py"

    def write(name: str, content: str) -> None:
        p = bin_dir / name
        p.write_text(content, encoding="utf-8")
        p.chmod(0o755)

    write(
        "nvidia-smi",
        "#!/bin/sh\necho 'NVIDIA RTX PRO 6000 Blackwell'\n",
    )
    write("ffmpeg", "#!/bin/sh\nexit 0\n")
    write(
        "npa",
        """#!/bin/sh
case "$*" in
  *"nurec check"*) echo '{"status":"ok","has_rt_cores":true}' ;;
  *"nurec fetch"*) echo '{"status":"ok","ncore_json":"/tmp/n.json","poses_component_group":"npa_rig","reference_camera":"cam1"}' ;;
  *"nurec reconstruct"*) echo '{"status":"ok","usdz_path":"/tmp/last.usdz","metrics":{"test/psnr":31.19,"test/ssim":0.833,"test/lpips":0.267}}' ;;
  *"nurec render"*) echo '{"status":"ok"}' ;;
  *"nurec visualize"*) echo '{"status":"completed"}' ;;
  *"nurec finalize"*) echo '{"status":"ok","has_usdz":true,"has_rrd":true,"artifact_count":42}' ;;
  *) echo '{}' ;;
esac
""",
    )
    # Intercept only `python3 -m pip ...` (a no-op) so the setup's dep install
    # passes through; everything else runs the real interpreter.
    real_py = sys.executable
    write(
        "python3",
        '#!/bin/sh\nif [ "$1" = "-m" ] && [ "$2" = "pip" ]; then exit 0; fi\n'
        f'exec "{real_py}" "$@"\n',
    )

    (boost / "npa").mkdir(parents=True)
    (boost / "npa" / "clients").mkdir(parents=True)
    (boost / "npa" / "__init__.py").write_text("", encoding="utf-8")
    (boost / "npa" / "clients" / "__init__.py").write_text("", encoding="utf-8")
    (boost / "npa" / "clients" / "storage.py").write_text(
        "class StorageClient:\n"
        "    @staticmethod\n"
        "    def from_environment():\n"
        "        return StorageClient()\n"
        "    def upload_file(self, local, uri):\n"
        "        print(f'UPLOAD {local} -> {uri}')\n"
        "        return uri\n",
        encoding="utf-8",
    )

    shell = living_lab.build_living_lab_workflow_spec()["states"][
        "zone-toro-standard-b"
    ]["run"]["shell"]
    zone_cfg = {
        "config.zone_name": "toro-standard-b",
        "config.run_prefix_uri": "s3://bucket/prefix/",
        "config.nurec_image": "nvcr.io/nvidia/nre/nre-ga:26.04",
        "config.rig_translation_offset": "0,0.25,0",
        "config.rig_rotation_offset": "0,0,0",
    }
    for tok, val in zone_cfg.items():
        shell = shell.replace("{{" + tok + "}}", val)
    script = tmp_path / "shard.sh"
    script.write_text(shell, encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["PYTHONPATH"] = str(boost)
    env["AWS_ACCESS_KEY_ID"] = "TESTKEY"
    env["AWS_SECRET_ACCESS_KEY"] = "TESTSECRET"
    proc = subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "UPLOAD" in proc.stdout
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["zone_name"] == "toro-standard-b"
    assert payload["status"] == "ok"
    assert "RTX PRO 6000" in payload["gpu_name"]
    assert payload["usdz_path"] == "/tmp/last.usdz"
    assert payload["finalize"]["has_usdz"] is True
    assert payload["finalize"]["artifact_count"] == 42
    assert payload["metrics"]["test/ssim"] == 0.833
