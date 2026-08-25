"""Backend-neutral workflow runtime commands for NPA agents."""

from __future__ import annotations

from enum import Enum
import json

import typer

from npa.agent_backend.workflow_runtime import (
    WorkflowRuntimeError,
    prepare_workflow_runtime,
    stop_workflow_runtime,
    workflow_runtime_status,
)
from npa.lifecycle_intent import OperationIntent, intent_boundary, json_stdout_contract


class OutputFormat(str, Enum):
    text = "text"
    json = "json"


app = typer.Typer(
    name="workflow-runtime",
    help="Prepare, inspect, and stop an isolated NPA workflow runtime.",
    no_args_is_help=True,
)


def _emit(payload: dict[str, object], output_format: OutputFormat) -> None:
    if output_format == OutputFormat.json:
        typer.echo(json.dumps(payload, sort_keys=True))
    else:
        typer.echo(
            f"workflow runtime: {payload['status']} "
            f"target_ready={str(payload['target_ready']).lower()}"
        )


def _failure_payload(exc: WorkflowRuntimeError) -> dict[str, object]:
    return {
        "schema": "npa.agent.workflow-runtime.v1",
        "status": "failed",
        "runtime_ready": False,
        "target_ready": False,
        "context_bound": False,
        "reused": False,
        "diagnostic_code": exc.code,
        "diagnostic": str(exc),
    }


@app.command("prepare")
@intent_boundary(OperationIntent.ENSURE_PRESENT)
@json_stdout_contract
def prepare_cmd(
    project: str = typer.Option(..., "--project", "-p", help="Exact NPA project alias."),
    cluster: str = typer.Option(..., "--cluster", help="Exact NPA workflow target."),
    scope: str = typer.Option(..., "--scope", help="Owner-scoped operation digest."),
    output_format: OutputFormat = typer.Option(OutputFormat.text, "--output-format"),
) -> None:
    """Prepare an isolated runtime and verify its exact workflow target."""

    try:
        result = prepare_workflow_runtime(project=project, cluster=cluster, scope=scope)
    except WorkflowRuntimeError as exc:
        _emit(_failure_payload(exc), output_format)
        raise typer.Exit(1) from exc
    _emit(result.to_dict(), output_format)


@app.command("status")
@intent_boundary(OperationIntent.OBSERVE)
@json_stdout_contract
def status_cmd(
    cluster: str = typer.Option(..., "--cluster", help="Exact NPA workflow target."),
    scope: str = typer.Option(..., "--scope", help="Owner-scoped operation digest."),
    output_format: OutputFormat = typer.Option(OutputFormat.text, "--output-format"),
) -> None:
    """Inspect one exact workflow runtime without changing it."""

    try:
        result = workflow_runtime_status(cluster=cluster, scope=scope)
    except WorkflowRuntimeError as exc:
        _emit(_failure_payload(exc), output_format)
        raise typer.Exit(1) from exc
    _emit(result.to_dict(), output_format)
    if result.status != "ready":
        raise typer.Exit(1)


@app.command("stop")
@intent_boundary(OperationIntent.DESTROY)
@json_stdout_contract
def stop_cmd(
    cluster: str = typer.Option(..., "--cluster", help="Exact NPA workflow target."),
    scope: str = typer.Option(..., "--scope", help="Owner-scoped operation digest."),
    yes: bool = typer.Option(False, "--yes", help="Confirm exact runtime teardown."),
    output_format: OutputFormat = typer.Option(OutputFormat.text, "--output-format"),
) -> None:
    """Stop only the isolated workflow runtime in this owner scope."""

    if not yes:
        raise typer.BadParameter("--yes is required to stop the workflow runtime")
    try:
        result = stop_workflow_runtime(cluster=cluster, scope=scope)
    except WorkflowRuntimeError as exc:
        _emit(_failure_payload(exc), output_format)
        raise typer.Exit(1) from exc
    _emit(result.to_dict(), output_format)


__all__ = ["app", "prepare_cmd", "status_cmd", "stop_cmd"]
