"""Supported Antioch service + tmux supervision for the OpenPI live example."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

import yaml

from .runtime import ensure_runtime
from .vendor_cli import AntiochCli, AntiochCliError

REMOTE_CLIENT_ROOT = "/workspace/npa-live-client"
REQUIRED_BUNDLE_FILES = ("ca.crt", "api-key", "endpoint.json")


class AntiochLiveError(RuntimeError):
    """The continuing live-session controller failed closed."""


def live_state_root() -> Path:
    configured = os.environ.get("NPA_ANTIOCH_LIVE_STATE_DIR", "").strip()
    if configured:
        return Path(configured)
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    return (Path(state_home) if state_home else Path.home() / ".local/state") / (
        "npa/antioch-live"
    )


def _session_name(project_id: str) -> str:
    digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:12]
    return f"npa-antioch-live-{digest}"


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["tmux", *args],
            text=True,
            capture_output=True,
            check=check,
        )
    except FileNotFoundError as exc:
        raise AntiochLiveError(
            "tmux is required for continuing Antioch sessions"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise AntiochLiveError("tmux live-session operation failed") from exc


def _session_running(name: str) -> bool:
    return _tmux("has-session", "-t", name, check=False).returncode == 0


def _state_path(project_id: str) -> Path:
    return live_state_root() / _session_name(project_id) / "state.json"


def _read_state(project_id: str) -> dict[str, Any]:
    path = _state_path(project_id)
    if not path.is_file() or path.is_symlink():
        raise AntiochLiveError("no exact live-session state exists for this project")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("project_id") != project_id:
        raise AntiochLiveError("live-session state identity is malformed")
    return value


def _validate_bundle(bundle: Path) -> None:
    for name in REQUIRED_BUNDLE_FILES:
        path = bundle / name
        if not path.is_file() or path.is_symlink():
            raise AntiochLiveError(f"client bundle is missing regular file {name!r}")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise AntiochLiveError(
                f"client bundle file {name!r} must not be group/world accessible"
            )
    endpoint = json.loads((bundle / "endpoint.json").read_text(encoding="utf-8"))
    if not isinstance(endpoint, dict) or endpoint.get("scheme") != "wss":
        raise AntiochLiveError("client bundle endpoint must use wss")
    if len((bundle / "api-key").read_text(encoding="utf-8").strip()) < 32:
        raise AntiochLiveError("client bundle API key is malformed")


def _stage_project(source: Path, destination: Path, project_id: str) -> None:
    if not (source / "antioch.yaml").is_file():
        raise AntiochLiveError("live source is not an Antioch project")
    shutil.copytree(source, destination)
    manifest_path = destination / "antioch.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("id") != "replace-at-runtime":
        raise AntiochLiveError(
            "live source must retain its non-deployable project placeholder"
        )
    manifest["id"] = project_id
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    os.chmod(manifest_path, 0o600)


def _write_supervisor(
    path: Path,
    *,
    cli_path: Path,
    client_bundle: Path,
    stop_file: Path,
    scenario_timeout_seconds: int,
) -> None:
    if scenario_timeout_seconds < 60:
        raise AntiochLiveError(
            "the per-run renewal boundary must be at least 60 seconds"
        )
    command = shlex.join(
        [
            str(cli_path),
            "scenario",
            "run",
            "--scenario",
            "openpi_droid_live",
            "--timeout",
            str(scenario_timeout_seconds),
            "--stream",
            "--verbose",
        ]
    )
    remote_files = [f"{REMOTE_CLIENT_ROOT}/{name}" for name in REQUIRED_BUNDLE_FILES]
    bundle_check = shlex.join(
        [
            str(cli_path),
            "services",
            "exec",
            "sim",
            "/bin/sh",
            "-lc",
            "test -r /workspace/npa-live-client/ca.crt "
            "-a -r /workspace/npa-live-client/api-key "
            "-a -r /workspace/npa-live-client/endpoint.json",
        ]
    )
    stage_commands = [
        shlex.join(
            [
                str(cli_path),
                "services",
                "exec",
                "sim",
                "install",
                "-d",
                "-m",
                "0700",
                REMOTE_CLIENT_ROOT,
            ]
        ),
        *[
            shlex.join(
                [
                    str(cli_path),
                    "services",
                    "cp",
                    str(client_bundle / name),
                    f"sim:{REMOTE_CLIENT_ROOT}/{name}",
                    "--json",
                ]
            )
            for name in REQUIRED_BUNDLE_FILES
        ],
        shlex.join(
            [
                str(cli_path),
                "services",
                "exec",
                "sim",
                "chmod",
                "0600",
                *remote_files,
            ]
        ),
    ]
    stage_block = " &&\n      ".join(stage_commands)
    content = f"""#!/bin/sh
set -u
while [ ! -f {shlex.quote(str(stop_file))} ]; do
  (
    sleep 5
    while [ ! -f {shlex.quote(str(stop_file))} ]; do
      if ! {bundle_check} >/dev/null 2>&1; then
        {{
          {stage_block}
        }} >/dev/null 2>&1 || true
      fi
      sleep 15
    done
  ) &
  restager=$!
  {command}
  status=$?
  kill "$restager" >/dev/null 2>&1 || true
  wait "$restager" >/dev/null 2>&1 || true
  if [ -f {shlex.quote(str(stop_file))} ]; then
    break
  fi
  printf 'NPA_ANTIOCH_RENEWAL exit_code=%s\n' "$status"
  sleep 5
done
"""
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o700)


def start_live(
    *,
    source: Path,
    project_id: str,
    client_bundle: Path,
    scenario_timeout_seconds: int = 14_400,
) -> dict[str, Any]:
    """Start the sim service and an indefinitely renewing streamed scenario."""

    if not project_id.strip() or project_id == "replace-at-runtime":
        raise AntiochLiveError("an assigned Antioch project ID is required")
    _validate_bundle(client_bundle)
    cli_path = ensure_runtime()
    cli = AntiochCli(cli_path)
    session = _session_name(project_id)
    if _session_running(session):
        return {"status": "already-running", "session": session}

    root = live_state_root() / session
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    runtime = root / f"runtime-{uuid.uuid4().hex}"
    _stage_project(source.resolve(), runtime, project_id)
    stop_file = runtime / ".stop"
    supervisor = runtime / ".supervise.sh"
    log = runtime / "live.log"
    _write_supervisor(
        supervisor,
        cli_path=cli_path,
        client_bundle=client_bundle.resolve(),
        stop_file=stop_file,
        scenario_timeout_seconds=scenario_timeout_seconds,
    )

    service_started = False
    try:
        cli.services_up(runtime)
        service_started = True
        cli.services_exec(
            runtime,
            "sim",
            ["install", "-d", "-m", "0700", REMOTE_CLIENT_ROOT],
        )
        for name in REQUIRED_BUNDLE_FILES:
            cli.services_copy(
                runtime,
                client_bundle / name,
                f"sim:{REMOTE_CLIENT_ROOT}/{name}",
            )
        cli.services_exec(
            runtime,
            "sim",
            [
                "chmod",
                "0600",
                *[f"{REMOTE_CLIENT_ROOT}/{name}" for name in REQUIRED_BUNDLE_FILES],
            ],
        )
        _tmux("new-session", "-d", "-s", session, "-c", str(runtime), str(supervisor))
        _tmux(
            "pipe-pane",
            "-o",
            "-t",
            f"{session}:0.0",
            f"exec >> {shlex.quote(str(log))} 2>&1",
        )
        time.sleep(2)
        if not _session_running(session):
            raise AntiochLiveError("the Antioch live supervisor exited during startup")
    except Exception:
        if _session_running(session):
            _tmux("kill-session", "-t", session, check=False)
        if service_started:
            try:
                cli.services_down(runtime)
            except AntiochCliError:
                pass
        raise

    state = {
        "schema": "npa.workbench.antioch-live.v1",
        "session": session,
        "runtime": str(runtime),
        "project_id": project_id,
        "scenario": "openpi_droid_live",
        "renewal_boundary_seconds": scenario_timeout_seconds,
        "service": "sim",
        "cli": str(cli_path),
    }
    state_path = root / "state.json"
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.chmod(state_path, 0o600)
    return {
        "status": "running",
        "session": session,
        "scenario": state["scenario"],
        "renewal_boundary_seconds": scenario_timeout_seconds,
        "credentials_in_process_arguments": False,
    }


def status_live(*, project_id: str) -> dict[str, Any]:
    """Return local supervisor state without reading auth storage or process lists."""

    state = _read_state(project_id)
    session = str(state["session"])
    runtime = Path(str(state["runtime"]))
    log = runtime / "live.log"
    return {
        "status": "running" if _session_running(session) else "stopped",
        "session": session,
        "scenario": state["scenario"],
        "renewal_boundary_seconds": state["renewal_boundary_seconds"],
        "log_exists": log.is_file(),
        "runtime": str(runtime),
    }


def stop_live(*, project_id: str, timeout_seconds: float = 120.0) -> dict[str, Any]:
    """Stop the exact streamed run first, then its exact supported sim service."""

    state = _read_state(project_id)
    session = str(state["session"])
    runtime = Path(str(state["runtime"]))
    stop_file = runtime / ".stop"
    stop_file.touch(mode=0o600, exist_ok=True)
    if _session_running(session):
        _tmux("send-keys", "-t", f"{session}:0.0", "C-c")
        deadline = time.monotonic() + timeout_seconds
        while _session_running(session) and time.monotonic() < deadline:
            time.sleep(1)
        if _session_running(session):
            raise AntiochLiveError(
                "scenario cancellation did not finish; refusing to tear down its service"
            )
    cli_path = Path(str(state["cli"]))
    AntiochCli(cli_path).services_down(runtime)
    return {
        "status": "stopped",
        "session": session,
        "service_stopped_after_scenario": True,
        "runtime_preserved": str(runtime),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--client-bundle", required=True)
    parser.add_argument("--scenario-timeout-seconds", type=int, default=14_400)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = start_live(
        source=Path(args.source),
        project_id=args.project_id,
        client_bundle=Path(args.client_bundle),
        scenario_timeout_seconds=args.scenario_timeout_seconds,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
