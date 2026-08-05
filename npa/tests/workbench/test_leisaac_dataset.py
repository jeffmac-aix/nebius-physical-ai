from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest
from PIL import Image

from npa.agent_backend.leisaac_registry import (
    REGISTRY_FINGERPRINT,
    registry_payload,
    validate_num_envs,
    validate_task,
)
from npa.workbench.leisaac.dataset import (
    DatasetError,
    EpisodeRecorder,
    S3DatasetStore,
    extract_step,
)


def _jpeg(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (1280, 720), color=color).save(output, format="JPEG")
    return output.getvalue()


def _step(index: int) -> dict:
    return {
        "observation.state": [float(index)] * 6,
        "action": [float(index) / 10] * 8,
        "reward": float(index),
        "terminated": False,
        "truncated": False,
        "done": False,
        "sim_step": index,
        "monotonic_ns": 1_000_000_000 + index,
        "wall_clock_ns": 2_000_000_000 + index,
        "input_id": index,
        "input_key": "W",
    }


def test_registry_is_the_honest_two_task_sequential_contract() -> None:
    payload = registry_payload()
    assert payload["fingerprint"] == REGISTRY_FINGERPRINT
    assert {task["task"] for task in payload["tasks"]} == {
        "LeIsaac-SO101-PickOrange-v0",
        "LeIsaac-SO101-LiftCube-v0",
    }
    assert payload["environment_model"] == "named-sequential"
    assert payload["max_parallel_environments"] == 1
    assert validate_task("LeIsaac-SO101-LiftCube-v0").endswith("LiftCube-v0")
    with pytest.raises(ValueError, match="unsupported"):
        validate_task("made-up")
    with pytest.raises(ValueError, match="exactly one"):
        validate_num_envs(2)


def test_extract_step_uses_real_environment_return_values() -> None:
    result = (
        {"policy": {"joint_pos": np.arange(6, dtype=np.float32)[None, :]}},
        np.array([0.75], dtype=np.float32),
        np.array([False]),
        np.array([True]),
        {"real": True},
    )
    record = extract_step(result, np.arange(8, dtype=np.float32)[None, :], sim_step=17)
    assert record["observation.state"] == pytest.approx(list(range(6)))
    assert record["action"] == pytest.approx(list(range(8)))
    assert record["reward"] == pytest.approx(0.75)
    assert record["terminated"] is False
    assert record["truncated"] is True
    assert record["done"] is True
    assert record["sim_step"] == 17
    assert record["monotonic_ns"] > 0 and record["wall_clock_ns"] > 0


def test_recorder_requires_outcome_and_atomically_finalizes(tmp_path: Path) -> None:
    published = []

    def publish(path: Path, metadata: dict) -> dict:
        published.append((path, metadata, path.joinpath("records.jsonl").read_text()))
        return {
            "episode_index": 3,
            "completed_episode_count": 4,
            "dataset_version_uri": "s3://bucket/demos/versions/v000004-test",
        }

    recorder = EpisodeRecorder(
        root=tmp_path,
        output_uri="s3://bucket/demos",
        task="LeIsaac-SO101-PickOrange-v0",
        environment_id="counter-a",
        environment_index=2,
        seed=7,
        run_id="run-1",
        source_commit="1" * 40,
        publisher=publish,
    )
    recorder.start()
    for index, color in ((1, (200, 10, 10)), (2, (10, 200, 10))):
        recorder.observe(_step(index))
        recorder.frame(_jpeg(color))
    with pytest.raises(DatasetError, match="mark success or failure"):
        recorder.finalize()
    recorder.mark("success")
    result = recorder.finalize()
    status = recorder.status()
    assert result["episode_index"] == 3
    assert status["state"] == "idle"
    assert status["active_episode"] is None
    assert status["last_episode_index"] == 3
    assert status["completed_episode_count"] == 4
    assert status["last_outcome"] == "success"
    assert len(published) == 1
    rows = [json.loads(line) for line in published[0][2].splitlines()]
    assert [row["sim_step"] for row in rows] == [1, 2]
    assert all(row["task"] == "LeIsaac-SO101-PickOrange-v0" for row in rows)


def test_recorder_can_retry_a_failed_immutable_upload(tmp_path: Path) -> None:
    attempts = 0

    def publish(_path: Path, _metadata: dict) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DatasetError("temporary object-store failure")
        return {
            "episode_index": 0,
            "completed_episode_count": 1,
            "dataset_version_uri": "s3://bucket/demos/versions/v000001-retry",
        }

    recorder = EpisodeRecorder(
        root=tmp_path,
        output_uri="s3://bucket/demos",
        task="LeIsaac-SO101-PickOrange-v0",
        environment_id="counter-a",
        environment_index=0,
        seed=7,
        run_id="run-1",
        source_commit="1" * 40,
        publisher=publish,
    )
    recorder.start()
    for index, color in ((1, (200, 10, 10)), (2, (10, 200, 10))):
        recorder.observe(_step(index))
        recorder.frame(_jpeg(color))
    recorder.mark("failure")
    with pytest.raises(DatasetError, match="temporary"):
        recorder.finalize()
    assert recorder.status()["state"] == "upload-failed"
    assert recorder.finalize()["episode_index"] == 0
    assert recorder.status()["state"] == "idle"


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}

    def put_object(
        self, *, Bucket, Key, Body, Metadata=None, IfNoneMatch=None, **_kwargs
    ):
        target = (Bucket, Key)
        if IfNoneMatch == "*" and target in self.objects:
            raise RuntimeError("precondition failed")
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[target] = (data, dict(Metadata or {}))
        return {"ETag": "test"}

    def list_objects_v2(self, *, Bucket, Prefix, **_kwargs):
        contents = [
            {"Key": key, "Size": len(value[0])}
            for (bucket, key), value in sorted(self.objects.items())
            if bucket == Bucket and key.startswith(Prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)][0])}

    def download_file(self, bucket, key, destination):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(self.objects[(bucket, key)][0])


def _episode_dir(
    root: Path, task: str, environment: str, number: int
) -> tuple[Path, dict]:
    episode = root / f"episode-{number}"
    frames = episode / "frames"
    frames.mkdir(parents=True)
    records = []
    for index, color in ((1, (200, 20, 20)), (2, (20, 200, 20)), (3, (20, 20, 200))):
        jpeg = _jpeg(color)
        (frames / f"frame-{index - 1:06d}.jpg").write_bytes(jpeg)
        row = _step(index)
        row.update(
            {
                "frame_sha256": __import__("hashlib").sha256(jpeg).hexdigest(),
                "task": task,
                "environment_id": environment,
                "environment_index": number,
                "seed": 40 + number,
            }
        )
        records.append(row)
    (episode / "records.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in records), encoding="utf-8"
    )
    metadata = {
        "schema": "npa.leisaac.episode.v1",
        "episode_uuid": f"episode-{number}",
        "run_id": f"run-{number}",
        "task": task,
        "environment_id": environment,
        "environment_index": number,
        "seed": 40 + number,
        "outcome": "success" if number == 0 else "failure",
        "frame_count": len(records),
        "fps": 16,
        "source_commit": "1" * 40,
        "recorded_at": "2026-08-05T00:00:00Z",
    }
    (episode / "episode.json").write_text(json.dumps(metadata), encoding="utf-8")
    return episode, metadata


def test_s3_store_resumes_episode_numbers_and_publishes_lerobot_v3(
    tmp_path: Path,
) -> None:
    fake = _FakeS3()
    store = S3DatasetStore("s3://bucket/demos/leisaac", client=fake)
    first = _episode_dir(tmp_path, "LeIsaac-SO101-PickOrange-v0", "kitchen-a", 0)
    second = _episode_dir(tmp_path, "LeIsaac-SO101-LiftCube-v0", "table-b", 1)
    result0 = store.publish_episode(*first)
    retried0 = store.publish_episode(*first)
    result1 = store.publish_episode(*second)
    assert result0["episode_index"] == 0
    assert retried0["episode_index"] == 0
    assert retried0["completed_episode_count"] == 1
    assert retried0["dataset_version_uri"] != result0["dataset_version_uri"]
    assert result1["episode_index"] == 1
    assert result1["completed_episode_count"] == 2
    assert result0["dataset_version_uri"] != result1["dataset_version_uri"]
    commits = [
        key for bucket, key in fake.objects if bucket == "bucket" and "/commits/" in key
    ]
    assert commits == [
        "demos/leisaac/commits/episode-000000.json",
        "demos/leisaac/commits/episode-000001.json",
    ]
    version_prefix = result1["dataset_version_uri"].split("s3://bucket/", 1)[1]
    info = json.loads(fake.objects[("bucket", f"{version_prefix}/meta/info.json")][0])
    assert info["codebase_version"] == "v3.0"
    assert info["total_episodes"] == 2
    assert info["total_tasks"] == 2
    assert info["features"]["observation.state"]["names"] == [
        "shoulder_pan",
        "shoulder_lift",
        "elbow_flex",
        "wrist_flex",
        "wrist_roll",
        "gripper",
    ]
    assert info["features"]["action"]["names"][-2:] == [
        "delta_shoulder_pan",
        "delta_gripper",
    ]
    assert info["features"]["observation.images.front"]["info"]["video.codec"] == "h264"
    tasks_bytes = fake.objects[("bucket", f"{version_prefix}/meta/tasks.parquet")][0]
    tasks = pd.read_parquet(io.BytesIO(tasks_bytes))
    assert list(tasks.index) == [
        "LeIsaac-SO101-LiftCube-v0",
        "LeIsaac-SO101-PickOrange-v0",
    ]
    assert list(tasks["task_index"]) == [0, 1]
    parquet_bytes = fake.objects[
        ("bucket", f"{version_prefix}/data/chunk-000/file-001.parquet")
    ][0]
    table = pq.read_table(io.BytesIO(parquet_bytes))
    assert table.num_rows == 3
    assert table["environment.id"].to_pylist() == ["table-b"] * 3
    assert table["success"].to_pylist() == [False, False, False]
    assert table["reset_reason"].to_pylist()[-1] == "failure"
