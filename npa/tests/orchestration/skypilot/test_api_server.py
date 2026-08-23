from __future__ import annotations

import json
from pathlib import Path

import pytest

from npa.orchestration.skypilot import api_server


class _Process:
    pid = 4242
    returncode = None

    def poll(self):
        return None


def test_ensure_starts_exact_loopback_server_and_writes_owner_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sky = tmp_path / "venv" / "bin" / "sky"
    python = sky.with_name("python")
    sky.parent.mkdir(parents=True)
    sky.write_text("#!/bin/sh\n", encoding="utf-8")
    sky.chmod(0o755)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("contexts: []\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def popen(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return _Process()

    monkeypatch.setattr(api_server, "ensure_skypilot_version", lambda _path: sky)
    monkeypatch.setattr(api_server.subprocess, "Popen", popen)
    monkeypatch.setattr(api_server, "_healthy", lambda _endpoint: True)
    monkeypatch.setattr(api_server, "_port_available", lambda _port: True)

    state_dir = tmp_path / "task" / "sky-api"
    result = api_server.ensure_isolated_api_server(
        sky_bin=sky, state_dir=state_dir, port=48123, kubeconfig=kubeconfig
    )

    assert result.endpoint == "http://127.0.0.1:48123"
    assert result.reused is False
    assert seen["argv"] == [
        str(python),
        "-c",
        (
            "import runpy,sys;"
            "from sky.server.requests.queues import mp_queue;"
            "mp_queue.DEFAULT_QUEUE_MANAGER_PORT=int(sys.argv.pop(1));"
            "runpy.run_module('sky.server.server',run_name='__main__')"
        ),
        "49123",
        "--host",
        "127.0.0.1",
        "--port",
        "48123",
    ]
    record = json.loads((state_dir / "server.json").read_text(encoding="utf-8"))
    assert record["pid"] == 4242
    assert (state_dir / "server.json").stat().st_mode & 0o777 == 0o600
    assert seen["kwargs"]["start_new_session"] is True


def test_ensure_refuses_an_occupied_unowned_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(api_server, "_port_available", lambda _port: False)
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("contexts: []\n", encoding="utf-8")

    with pytest.raises(api_server.IsolatedApiServerError, match="unowned process"):
        api_server.ensure_isolated_api_server(
            sky_bin=tmp_path / "sky",
            state_dir=tmp_path / "task" / "sky-api",
            port=48123,
            kubeconfig=kubeconfig,
        )


def test_stop_absent_server_is_non_destructive(tmp_path: Path) -> None:
    result = api_server.stop_isolated_api_server(
        state_dir=tmp_path / "task" / "sky-api"
    )

    assert result["status"] == "absent"
    assert result["stopped"] is False
