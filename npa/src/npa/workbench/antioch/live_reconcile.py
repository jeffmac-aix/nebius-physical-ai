"""Reconcile an accepted Antioch live run after the foreground CLI detaches."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Sequence

import yaml

from .vendor_cli import AntiochCli

LIVE_PHASES = {"queued", "booting", "running"}
TERMINAL_STREAM_STATES = {"failed", "stopped", "idle"}
NO_ACTIVE_RUN = 3


class AntiochLiveReconcileError(RuntimeError):
    """The supported run inventory could not identify one exact live run."""


def _project_id(runtime: Path) -> str:
    manifest = yaml.safe_load((runtime / "antioch.yaml").read_text(encoding="utf-8"))
    project_id = str((manifest or {}).get("id") or "").strip()
    if not project_id or project_id == "replace-at-runtime":
        raise AntiochLiveReconcileError("runtime project identity is unavailable")
    return project_id


def _active_run(
    cli: AntiochCli,
    *,
    runtime: Path,
    project_id: str,
    scenario: str = "openpi_droid_live",
) -> dict[str, Any] | None:
    rows = cli.list_for_project(runtime, kind="scenario", project_id=project_id)
    candidates = {
        str(row["scenario_run_id"]): row
        for row in rows
        if row.get("scenario") == scenario
        and row.get("phase") in LIVE_PHASES
        and row.get("scenario_run_id")
    }
    machine = cli.machine_status(runtime, project_id=project_id)
    stream = machine.get("stream") or {}
    stream_run_id = str(stream.get("scenario_run_id") or "")
    stream_state = str(stream.get("state") or "").lower()
    if stream_run_id and stream_state not in TERMINAL_STREAM_STATES:
        selected = candidates.get(stream_run_id)
        if selected is None:
            raise AntiochLiveReconcileError(
                "active stream owner is absent from the exact project run inventory"
            )
        return selected
    if len(candidates) > 1:
        raise AntiochLiveReconcileError(
            "multiple exact live runs are active; refusing ambiguous adoption"
        )
    return next(iter(candidates.values()), None)


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, (json.dumps(payload, sort_keys=True) + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def reconcile_active(
    *,
    cli_path: Path,
    runtime: Path,
    stop_file: Path,
    state_path: Path,
    scenario: str = "openpi_droid_live",
    poll_seconds: float = 5.0,
) -> bool:
    """Wait on one exact accepted run; return False when there is none."""

    cli = AntiochCli(cli_path)
    project_id = _project_id(runtime)
    active = _active_run(
        cli, runtime=runtime, project_id=project_id, scenario=scenario
    )
    if active is None:
        return False
    remote_id = str(active["scenario_run_id"])
    _write_state(
        state_path,
        {
            "schema": "npa.workbench.antioch-live-active.v1",
            "scenario": scenario,
            "scenario_run_id": remote_id,
            "status": "reconciled",
        },
    )
    print("NPA_ANTIOCH_RECONCILED_ACTIVE", flush=True)
    while True:
        if stop_file.exists():
            cli.cancel(runtime, kind="scenario", remote_id=remote_id)
        current = _active_run(
            cli, runtime=runtime, project_id=project_id, scenario=scenario
        )
        if current is None:
            _write_state(
                state_path,
                {
                    "schema": "npa.workbench.antioch-live-active.v1",
                    "scenario": scenario,
                    "scenario_run_id": remote_id,
                    "status": "terminal",
                },
            )
            return True
        current_id = str(current["scenario_run_id"])
        if current_id != remote_id:
            raise AntiochLiveReconcileError(
                "the active live run changed during reconciliation"
            )
        time.sleep(poll_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--stop-file", required=True)
    parser.add_argument("--state-path", required=True)
    parser.add_argument("--scenario", default="openpi_droid_live")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    adopted = reconcile_active(
        cli_path=Path(args.cli),
        runtime=Path(args.runtime),
        stop_file=Path(args.stop_file),
        state_path=Path(args.state_path),
        scenario=args.scenario,
        poll_seconds=args.poll_seconds,
    )
    return 0 if adopted else NO_ACTIVE_RUN


if __name__ == "__main__":
    raise SystemExit(main())
