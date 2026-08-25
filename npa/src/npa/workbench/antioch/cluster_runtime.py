"""PID-1 controller for the cluster-native Antioch live adapter pod.

The supported Antioch service tunnel is created in this pod.  A sibling relay
container shares the pod network namespace and connects to the tunnel only on
localhost, so the operator VM never carries frame or action traffic.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from .live import (
    AntiochLiveError,
    _cancel_remote_live_runs,
    _stage_private_bundle,
    _stage_project,
    _stage_runtime_source,
    _validate_bundle,
    _write_supervisor,
)
from .runtime import ensure_runtime
from .vendor_cli import AntiochCli, AntiochCliError


SCHEMA = "npa.workbench.antioch-cluster-live.v1"


def _write_state(path: Path, **values: Any) -> None:
    state = {"schema": SCHEMA, **values}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, (json.dumps(state, sort_keys=True) + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _private_value(path: Path, *, label: str) -> str:
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise AntiochLiveError(f"private {label} file is unavailable")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise AntiochLiveError(f"private {label} file is empty")
    return value


def run_cluster(args: argparse.Namespace) -> int:
    private_root = Path(args.private_root)
    bundle = private_root / "live-bundle"
    _validate_bundle(bundle)
    project_id = _private_value(private_root / "project-id", label="project identity")
    accepted = _private_value(private_root / "antioch-terms", label="terms acceptance")
    if accepted != "YES":
        raise AntiochLiveError("Antioch terms acceptance is not the exact required value")
    os.environ["NPA_ANTIOCH_ACCEPT_TERMS"] = accepted
    os.environ["ANTIOCH_CONFIG_DIR"] = str(private_root / "antioch-config")

    state_path = Path(args.state_path)
    root = Path(args.runtime_root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    runtime = root / f"runtime-{uuid.uuid4().hex}"
    _stage_project(Path(args.source).resolve(), runtime, project_id)
    stop_file = Path(args.stop_file)
    supervisor = runtime / ".supervise.sh"
    active_state = runtime / "active-run.json"
    cli_path = ensure_runtime()
    cli = AntiochCli(cli_path, config_dir=str(private_root / "antioch-config"))
    _write_supervisor(
        supervisor,
        cli_path=Path(cli_path),
        python_path=Path(sys.executable),
        client_bundle=bundle,
        stop_file=stop_file,
        active_state_path=active_state,
        scenario_timeout_seconds=args.scenario_timeout_seconds,
        scenario_name=args.scenario,
    )

    process: subprocess.Popen[str] | None = None
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        stop_file.touch(mode=0o600, exist_ok=True)
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    service_started = False
    try:
        _write_state(state_path, status="starting", scenario=args.scenario)
        cli.services_build(runtime, service="sim")
        cli.services_up(runtime)
        service_started = True
        _stage_runtime_source(cli, runtime=runtime)
        _stage_private_bundle(cli, runtime=runtime, client_bundle=bundle)
        process = subprocess.Popen(
            [str(supervisor)], cwd=runtime, text=True, start_new_session=False
        )
        _write_state(
            state_path,
            status="running",
            scenario=args.scenario,
            transport="same-pod-antioch-tunnel-double-wss",
            dev_vm_in_data_path=False,
        )
        while process.poll() is None:
            if stop_file.exists() and not stopping:
                request_stop(signal.SIGTERM, None)
            time.sleep(1)
        if not stopping:
            raise AntiochLiveError("cluster live supervisor exited unexpectedly")
    except Exception as exc:
        _write_state(
            state_path,
            status="failed",
            scenario=args.scenario,
            error_type=type(exc).__name__,
        )
        raise
    finally:
        stop_file.touch(mode=0o600, exist_ok=True)
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if service_started:
            try:
                _cancel_remote_live_runs(
                    cli,
                    runtime=runtime,
                    project_id=project_id,
                    scenario=args.scenario,
                    attempts=5,
                )
                cli.services_down(runtime)
            except (AntiochCliError, AntiochLiveError) as exc:
                _write_state(
                    state_path,
                    status="cleanup_failed",
                    scenario=args.scenario,
                    error_type=type(exc).__name__,
                )
                raise
        _write_state(state_path, status="stopped", scenario=args.scenario)
    return 0


def probe(path: Path, *, component: str) -> int:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 1
    status = str(state.get("status") or "")
    allowed = {"running"} if component == "controller" else {"connected"}
    return 0 if status in allowed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--source", default="/opt/npa/antioch-openpi-live")
    run.add_argument("--private-root", default="/run/npa-antioch-private")
    run.add_argument("--runtime-root", default="/var/lib/npa-antioch-live")
    run.add_argument("--state-path", default="/var/run/npa-antioch/controller.json")
    run.add_argument("--stop-file", default="/var/run/npa-antioch/stop")
    run.add_argument("--scenario", default="openpi_franka_mk8s_live")
    run.add_argument("--scenario-timeout-seconds", type=int, default=14_400)
    check = subparsers.add_parser("probe")
    check.add_argument("--state-path", required=True)
    check.add_argument("--component", choices=("controller", "relay"), required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "probe":
        return probe(Path(args.state_path), component=args.component)
    return run_cluster(args)


if __name__ == "__main__":
    raise SystemExit(main())
