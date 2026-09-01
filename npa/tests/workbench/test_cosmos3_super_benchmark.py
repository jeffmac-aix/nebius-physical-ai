from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from npa.workbench.cosmos import super_benchmark as benchmark


def _valid_record(name: str, latency: float = 2.0) -> dict:
    return {
        "attempt_id": name,
        "http_status": 200,
        "output_bytes": 10,
        "technical_valid": True,
        "failure_reason": None,
        "client_wall_seconds": latency,
        "validation": {"valid": True},
    }


def test_plan_pins_primary_contract() -> None:
    plan = benchmark.benchmark_plan(
        output_path="s3://example-bucket/run/",
        topologies="1x8,2x4,4x2,8x1",
        attempts=24,
    )
    assert plan["runtime_image"] == benchmark.IMAGE
    assert plan["model"]["revision"] == benchmark.MODEL_REVISION
    assert plan["workload"] == benchmark.WORKLOAD
    assert [cell["services"] for cell in plan["topologies"]] == [1, 2, 4, 8]
    assert all(cell["request_concurrency_per_service"] == 1 for cell in plan["topologies"])
    assert all(cell["warmups_per_service"] == 1 for cell in plan["topologies"])
    assert plan["sync_timeout_seconds"] == 5400


def test_plan_rejects_uneven_service_distribution() -> None:
    with pytest.raises(benchmark.Cosmos3SuperBenchmarkError, match="divide evenly"):
        benchmark.benchmark_plan(
            output_path="/tmp/results", topologies="8x1", attempts=23
        )


def test_service_commands_fill_node_with_expected_parallelism() -> None:
    hybrid = benchmark.service_command(benchmark.TOPOLOGIES["1x8"], port=8100)
    assert hybrid[-1] == "--no-guardrails"
    assert hybrid[hybrid.index("--revision") + 1] == benchmark.MODEL_REVISION
    assert "--cfg-parallel-size" in hybrid
    assert "--ulysses-degree" in hybrid
    assert "--use-hsdp" in hybrid
    assert "--hsdp-shard-size" in hybrid
    for name, size in (("2x4", "4"), ("4x2", "2"), ("8x1", "1")):
        command = benchmark.service_command(benchmark.TOPOLOGIES[name], port=8100)
        assert command[command.index("--tensor-parallel-size") + 1] == size


def test_failed_attempt_keeps_window_time_and_gets_zero_credit() -> None:
    failed = _valid_record("failed", latency=9.0)
    failed.update(
        {
            "http_status": 500,
            "output_bytes": 0,
            "technical_valid": False,
            "failure_reason": "http_500",
            "validation": None,
        }
    )
    derived = benchmark.derive_cell([_valid_record("ok", 3.0), failed], 12.0)
    assert derived["attempts"] == 2
    assert derived["valid_attempts"] == 1
    assert derived["failed_attempts"] == 1
    assert derived["credited_valid_video_seconds"] == 7.875
    assert derived["valid_video_seconds_per_node_hour"] == 2362.5
    assert derived["window_seconds"] == 12.0


def test_dispatch_runs_one_request_at_a_time_per_service(tmp_path: Path) -> None:
    active: dict[int, int] = {}
    peaks: dict[int, int] = {}
    seeds: dict[int, list[int]] = {}
    lock = threading.Lock()

    def fake_attempt(**kwargs):
        replica = kwargs["replica"]
        with lock:
            active[replica] = active.get(replica, 0) + 1
            peaks[replica] = max(peaks.get(replica, 0), active[replica])
            seeds.setdefault(replica, []).append(kwargs["seed"])
        time.sleep(0.005)
        with lock:
            active[replica] -= 1
        row = _valid_record(kwargs["attempt_id"], latency=0.005)
        row.update(
            {
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
                "replica": replica,
            }
        )
        return row

    topology = benchmark.TOPOLOGIES["4x2"]
    records, window = benchmark.dispatch_cell(
        topology=topology,
        urls=[f"http://127.0.0.1:{8100 + index}" for index in range(4)],
        attempts=24,
        prompt="prompt",
        negative_prompt="negative",
        prompt_hashes={"prompt_sha256": "a", "negative_prompt_sha256": "b"},
        clips_dir=tmp_path,
        kind="production",
        attempt_fn=fake_attempt,
    )
    assert len(records) == 24
    assert set(peaks.values()) == {1}
    assert all(values == [17, 23, 41, 17, 23, 41] for values in seeds.values())
    assert window["seconds"] > 0
    assert "tail idle time" in window["boundary"]


def test_video_gate_checks_shape_blank_and_motion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(benchmark.shutil, "which", lambda _name: "/usr/bin/tool")
    stream = {
        "streams": [
            {
                "width": 1280,
                "height": 720,
                "avg_frame_rate": "24/1",
                "nb_read_frames": "189",
                "duration": "7.875",
            }
        ]
    }
    frames = b"".join(bytes((value + index) % 256 for value in range(256)) * 4 for index in range(5))

    def fake_run(command, *, binary=False):
        if "ffprobe" in command[0]:
            return subprocess.CompletedProcess(command, 0, json.dumps(stream), "")
        if binary:
            return subprocess.CompletedProcess(command, 0, frames, b"")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(benchmark, "_run", fake_run)
    result = benchmark.validate_video(tmp_path / "clip.mp4")
    assert result["valid"] is True
    assert result["stream"]["nb_read_frames"] == "189"
