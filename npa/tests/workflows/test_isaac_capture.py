"""Unit tests for `npa.workflows.isaac_capture`.

Isaac Sim itself cannot run here, so these cover the parts that broke live and are checkable
without a simulator: what gets published, and when.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from npa.workflows import isaac_capture


def test_publish_runs_before_the_simulator_closes(monkeypatch, tmp_path: Path) -> None:
    """Live job 278: six frames, exit 0, nothing uploaded.

    `simulation_app.close()` tears the process down rather than returning, so the upload that
    used to live in `main()` after `_capture_frames()` never happened — and the next stage
    failed with "No scene images found". The publish callback must fire first.
    """

    order: list[str] = []

    class FakeApp:
        def close(self) -> None:
            order.append("close")

    def fake_capture(*, task, output_dir, max_steps, max_frames, episodes, publish=None):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "frame_00.png").write_bytes(b"png")
        summary = {"status": "success", "frames": ["frame_00.png"], "task": task}
        if publish is not None:
            order.append("publish")
            publish(summary)
        FakeApp().close()
        return summary

    uploaded: dict[str, str] = {}
    monkeypatch.setattr(isaac_capture, "_capture_frames", fake_capture)
    monkeypatch.setattr(
        isaac_capture,
        "_upload_tree",
        lambda local, uri: uploaded.setdefault("uri", uri) and {} or {"frame_00.png": uri},
    )

    assert isaac_capture.main(["--output-path", "s3://bucket/scene/"]) == 0
    assert order == ["publish", "close"], "publish must happen before the app tears down"
    assert uploaded["uri"] == "s3://bucket/scene/"


def test_upload_carries_the_summary_next_to_the_frames(monkeypatch, tmp_path: Path) -> None:
    """Uploading only *.png stranded isaac_capture_summary.json in the pod."""

    (tmp_path / "frame_00.png").write_bytes(b"png")
    (tmp_path / "isaac_capture_summary.json").write_text(json.dumps({"status": "success"}))

    sent: list[str] = []

    class FakeS3:
        def upload_file(self, local: str, bucket: str, key: str) -> None:
            sent.append(key)

    class FakeBoto:
        @staticmethod
        def client(*_args, **_kwargs):
            return FakeS3()

    monkeypatch.setitem(__import__("sys").modules, "boto3", FakeBoto)

    isaac_capture._upload_tree(tmp_path, "s3://bucket/run/scene/")

    assert sorted(sent) == [
        "run/scene/frame_00.png",
        "run/scene/isaac_capture_summary.json",
    ]


def test_render_only_reports_the_resolved_settings_without_a_simulator(capsys) -> None:
    assert isaac_capture.main(["--output-path", "s3://bucket/scene/", "--render-only"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task"] == "Isaac-Lift-Cube-Franka-v0"
    assert payload["output_path"] == "s3://bucket/scene/"


def test_upload_rejects_a_non_s3_destination(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        isaac_capture._upload_tree(tmp_path, "gs://bucket/scene/")
