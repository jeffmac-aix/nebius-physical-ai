"""Narrow workbench image lifecycle commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from npa.lifecycle_intent import OperationIntent, intent_boundary, json_stdout_contract
from npa.workbench.rerun_image import (
    RerunImageError,
    build_rerun_viewer,
    inspect_rerun_viewer,
    push_rerun_viewer,
    verify_rerun_viewer,
)

app = typer.Typer(name="image", help="Build and verify task-owned workbench images.")


def _emit(payload: dict) -> None:  # noqa: ANN001
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def _guard(callable_, **kwargs) -> None:  # noqa: ANN001
    try:
        _emit(callable_(**kwargs))
    except RerunImageError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("build-rerun-viewer")
@intent_boundary(OperationIntent.MUTATE)
@json_stdout_contract
def build_cmd(
    project: str = typer.Option(..., "--project"),
    tag: str = typer.Option(..., "--tag"),
    repo_root: Path = typer.Option(Path.cwd(), "--repo-root", exists=True, file_okay=False),
) -> None:
    """Build the checked-in Rerun viewer into the local Docker engine."""

    _guard(build_rerun_viewer, project=project, tag=tag, repo_root=repo_root)


@app.command("inspect-rerun-viewer")
@intent_boundary(OperationIntent.OBSERVE)
@json_stdout_contract
def inspect_cmd(
    project: str = typer.Option(..., "--project"),
    tag: str = typer.Option(..., "--tag"),
) -> None:
    """Inspect and capability-probe the exact local Rerun image bytes."""

    _guard(inspect_rerun_viewer, project=project, tag=tag)


@app.command("push-rerun-viewer")
@intent_boundary(OperationIntent.MUTATE)
@json_stdout_contract
def push_cmd(
    project: str = typer.Option(..., "--project"),
    tag: str = typer.Option(..., "--tag"),
    expected_image_id: str = typer.Option(..., "--expected-image-id"),
    inspection_digest: str = typer.Option(..., "--inspection-digest"),
) -> None:
    """Push only a prior digest-bound compatible local Rerun image."""

    _guard(
        push_rerun_viewer,
        project=project,
        tag=tag,
        expected_image_id=expected_image_id,
        inspection_digest=inspection_digest,
    )


@app.command("verify-rerun-viewer")
@intent_boundary(OperationIntent.OBSERVE)
@json_stdout_contract
def verify_cmd(
    project: str = typer.Option(..., "--project"),
    tag: str = typer.Option(..., "--tag"),
    expected_digest: str = typer.Option(..., "--expected-digest"),
) -> None:
    """Pull and probe the exact digest that will be supplied to workflow preflight."""

    _guard(
        verify_rerun_viewer,
        project=project,
        tag=tag,
        expected_digest=expected_digest,
    )
