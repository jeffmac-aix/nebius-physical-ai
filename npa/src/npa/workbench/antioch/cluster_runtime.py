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
from .live_reconcile import _active_run_snapshot
from .runtime import ensure_runtime
from .vendor_cli import AntiochCli, AntiochCliError


SCHEMA = "npa.workbench.antioch-cluster-live.v2"
SCHEMA_VERSION = 2
DEFAULT_CONTROLLER_MAX_AGE_SECONDS = 30.0
DEFAULT_RELAY_MAX_AGE_SECONDS = 150.0
STATE_READ_ATTEMPTS = 3
DAEMON_POLL_SECONDS = 5.0
DAEMON_ABSENCE_THRESHOLD = 3
DAEMON_ERROR_THRESHOLD = 3
DAEMON_STARTUP_GRACE_SECONDS = 600.0


def _write_state(path: Path, **values: Any) -> None:
    state = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "published_unix": time.time(),
        **values,
    }
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


def _read_state(
    path: Path, *, attempts: int = STATE_READ_ATTEMPTS, delay_seconds: float = 0.05
) -> dict[str, Any]:
    """Read one atomically-published state with bounded transient recovery."""

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise TypeError("state is not an object")
            return parsed
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(delay_seconds)
    assert last_error is not None
    raise last_error


def _state_ready(
    state: dict[str, Any],
    *,
    component: str,
    expected_owner_identity: str,
    max_age_seconds: float,
    now: float | None = None,
) -> bool:
    if int(state.get("schema_version") or 0) != SCHEMA_VERSION:
        return False
    if str(state.get("owner_identity") or "") != expected_owner_identity:
        return False
    if component == "controller-liveness":
        published = state.get("published_unix")
        published_age = (
            (time.time() if now is None else now) - float(published)
            if isinstance(published, (int, float))
            and not isinstance(published, bool)
            else max_age_seconds + 1
        )
        return bool(
            -5.0 <= published_age <= max_age_seconds
            and state.get("status")
            in {"starting", "running", "degraded", "recovering"}
        )
    heartbeat = state.get("heartbeat_unix")
    if not isinstance(heartbeat, (int, float)) or isinstance(heartbeat, bool):
        return False
    age = (time.time() if now is None else now) - float(heartbeat)
    if age < -5.0 or age > max_age_seconds:
        return False
    if component == "controller":
        return bool(
            state.get("status") == "running"
            and state.get("daemon_status") == "owned"
            and str(state.get("scenario_run_id") or "")
            and str(state.get("session_id") or "")
        )
    if component == "relay-liveness":
        return state.get("status") in {
            "starting",
            "connecting_simulation",
            "connecting_policy",
            "connected",
            "reconnecting",
        }
    return state.get("status") == "connected"


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


def _supervisor_recovery_reason(
    *,
    child_dead: bool,
    last_owned_heartbeat: float,
    consecutive_absence: int,
    consecutive_errors: int,
    age_seconds: float,
    startup_age_seconds: float,
    max_age_seconds: float,
) -> str:
    """Classify only converged loss as replacement-worthy."""

    if child_dead:
        return "controller_child_exit"
    if (
        last_owned_heartbeat
        and consecutive_absence >= DAEMON_ABSENCE_THRESHOLD
        and age_seconds > max_age_seconds
    ):
        return "daemon_owner_absent"
    if (
        not last_owned_heartbeat
        and startup_age_seconds >= DAEMON_STARTUP_GRACE_SECONDS
    ):
        return "daemon_owner_startup_timeout"
    if consecutive_errors >= DAEMON_ERROR_THRESHOLD and age_seconds > max_age_seconds:
        return "daemon_state_unreadable"
    return ""


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
    # A container restart keeps the pod's emptyDir. A prior SIGTERM path may
    # have left its stop marker behind; the new owner session must not inherit it.
    stop_file.unlink(missing_ok=True)
    supervisor = runtime / ".supervise.sh"
    active_state = runtime / "active-run.json"
    session_id = uuid.uuid4().hex
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
        owner_identity=args.owner_identity,
        session_id=session_id,
    )

    process: subprocess.Popen[str] | None = None
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        stop_file.touch(mode=0o600, exist_ok=True)
        if process is not None and process.poll() is None:
            _terminate_process_group(process)

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    service_started = False
    try:
        _write_state(
            state_path,
            status="starting",
            daemon_status="awaiting_owner",
            owner_identity=args.owner_identity,
            session_id=session_id,
            scenario=args.scenario,
            heartbeat_unix=0.0,
        )
        cli.services_build(runtime, service="sim")
        cli.services_up(runtime)
        service_started = True
        _stage_runtime_source(cli, runtime=runtime)
        _stage_private_bundle(cli, runtime=runtime, client_bundle=bundle)
        process = subprocess.Popen(
            [str(supervisor)], cwd=runtime, text=True, start_new_session=True
        )
        supervisor_started = time.monotonic()
        last_owned_heartbeat = 0.0
        last_run_id = ""
        consecutive_absence = 0
        consecutive_errors = 0
        recoveries = 0
        while not stopping:
            if stop_file.exists() and not stopping:
                request_stop(signal.SIGTERM, None)
                break
            child_dead = process.poll() is not None
            recovery_reason = _supervisor_recovery_reason(
                child_dead=child_dead,
                last_owned_heartbeat=last_owned_heartbeat,
                consecutive_absence=consecutive_absence,
                consecutive_errors=consecutive_errors,
                age_seconds=(
                    time.time() - last_owned_heartbeat
                    if last_owned_heartbeat
                    else time.monotonic() - supervisor_started
                ),
                startup_age_seconds=time.monotonic() - supervisor_started,
                max_age_seconds=args.daemon_max_age_seconds,
            )
            if not child_dead:
                try:
                    active = _active_run_snapshot(
                        cli,
                        runtime=runtime,
                        project_id=project_id,
                        scenario=args.scenario,
                        require_stream_owner=True,
                    )
                    consecutive_errors = 0
                    if active is None:
                        consecutive_absence += 1
                        recovery_reason = _supervisor_recovery_reason(
                            child_dead=False,
                            last_owned_heartbeat=last_owned_heartbeat,
                            consecutive_absence=consecutive_absence,
                            consecutive_errors=consecutive_errors,
                            age_seconds=(
                                time.time() - last_owned_heartbeat
                                if last_owned_heartbeat
                                else time.monotonic() - supervisor_started
                            ),
                            startup_age_seconds=time.monotonic() - supervisor_started,
                            max_age_seconds=args.daemon_max_age_seconds,
                        )
                        _write_state(
                            state_path,
                            status=("degraded" if last_owned_heartbeat else "starting"),
                            daemon_status="owner_absent",
                            owner_identity=args.owner_identity,
                            session_id=session_id,
                            scenario=args.scenario,
                            scenario_run_id=last_run_id,
                            heartbeat_unix=last_owned_heartbeat,
                            recoveries=recoveries,
                        )
                    else:
                        consecutive_absence = 0
                        last_owned_heartbeat = time.time()
                        last_run_id = str(active["scenario_run_id"])
                        _write_state(
                            state_path,
                            status="running",
                            daemon_status="owned",
                            owner_identity=args.owner_identity,
                            session_id=session_id,
                            scenario=args.scenario,
                            scenario_run_id=last_run_id,
                            run_phase=str(active.get("phase") or ""),
                            stream_state=str(active.get("stream_state") or ""),
                            heartbeat_unix=last_owned_heartbeat,
                            recoveries=recoveries,
                            transport="same-pod-antioch-tunnel-double-wss",
                            dev_vm_in_data_path=False,
                        )
                except (AntiochCliError, AntiochLiveError, RuntimeError) as exc:
                    consecutive_errors += 1
                    age = (
                        time.time() - last_owned_heartbeat
                        if last_owned_heartbeat
                        else time.monotonic() - supervisor_started
                    )
                    recovery_reason = _supervisor_recovery_reason(
                        child_dead=False,
                        last_owned_heartbeat=last_owned_heartbeat,
                        consecutive_absence=consecutive_absence,
                        consecutive_errors=consecutive_errors,
                        age_seconds=age,
                        startup_age_seconds=time.monotonic() - supervisor_started,
                        max_age_seconds=args.daemon_max_age_seconds,
                    )
                    _write_state(
                        state_path,
                        status="degraded",
                        daemon_status="unreadable",
                        owner_identity=args.owner_identity,
                        session_id=session_id,
                        scenario=args.scenario,
                        scenario_run_id=last_run_id,
                        heartbeat_unix=last_owned_heartbeat,
                        error_type=type(exc).__name__,
                        recoveries=recoveries,
                    )
            if recovery_reason:
                recoveries += 1
                _write_state(
                    state_path,
                    status="recovering",
                    daemon_status="replacing_supervisor",
                    owner_identity=args.owner_identity,
                    session_id=session_id,
                    scenario=args.scenario,
                    scenario_run_id=last_run_id,
                    heartbeat_unix=last_owned_heartbeat,
                    recovery_reason=recovery_reason,
                    recoveries=recoveries,
                )
                _terminate_process_group(process)
                process = subprocess.Popen(
                    [str(supervisor)], cwd=runtime, text=True, start_new_session=True
                )
                supervisor_started = time.monotonic()
                consecutive_absence = 0
                consecutive_errors = 0
                last_owned_heartbeat = 0.0
                last_run_id = ""
            time.sleep(args.daemon_poll_seconds)
    except Exception as exc:
        _write_state(
            state_path,
            status="failed",
            daemon_status="failed",
            owner_identity=args.owner_identity,
            session_id=session_id,
            scenario=args.scenario,
            heartbeat_unix=time.time(),
            error_type=type(exc).__name__,
        )
        raise
    finally:
        stop_file.touch(mode=0o600, exist_ok=True)
        if process is not None and process.poll() is None:
            _terminate_process_group(process)
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
                    daemon_status="cleanup_failed",
                    owner_identity=args.owner_identity,
                    session_id=session_id,
                    scenario=args.scenario,
                    heartbeat_unix=time.time(),
                    error_type=type(exc).__name__,
                )
                raise
        _write_state(
            state_path,
            status="stopped",
            daemon_status="terminal",
            owner_identity=args.owner_identity,
            session_id=session_id,
            scenario=args.scenario,
            heartbeat_unix=time.time(),
        )
    return 0


def probe(
    path: Path,
    *,
    component: str,
    expected_owner_identity: str,
    max_age_seconds: float,
) -> int:
    try:
        state = _read_state(path)
    except (OSError, TypeError, json.JSONDecodeError):
        return 1
    return (
        0
        if _state_ready(
            state,
            component=component,
            expected_owner_identity=expected_owner_identity,
            max_age_seconds=max_age_seconds,
        )
        else 1
    )


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
    run.add_argument("--owner-identity", required=True)
    run.add_argument("--daemon-poll-seconds", type=float, default=DAEMON_POLL_SECONDS)
    run.add_argument(
        "--daemon-max-age-seconds",
        type=float,
        default=DEFAULT_CONTROLLER_MAX_AGE_SECONDS,
    )
    check = subparsers.add_parser("probe")
    check.add_argument("--state-path", required=True)
    check.add_argument(
        "--component",
        choices=("controller", "controller-liveness", "relay", "relay-liveness"),
        required=True,
    )
    check.add_argument("--expected-owner-identity", required=True)
    check.add_argument("--max-age-seconds", type=float, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "probe":
        return probe(
            Path(args.state_path),
            component=args.component,
            expected_owner_identity=args.expected_owner_identity,
            max_age_seconds=args.max_age_seconds,
        )
    return run_cluster(args)


if __name__ == "__main__":
    raise SystemExit(main())
