"""CLI tests for `npa workbench robocasa`."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from npa.cli.main import app as main_app
from npa.cli.workbench.robocasa import app as robocasa_app


runner = CliRunner()


def test_registered_under_workbench() -> None:
    result = runner.invoke(main_app, ["workbench", "robocasa", "--help"])
    assert result.exit_code == 0
    assert "RoboCasa kitchen-task simulation workbench" in result.stdout


def test_help_lists_commands() -> None:
    result = runner.invoke(robocasa_app, ["--help"])
    assert result.exit_code == 0
    for command in ("deploy", "run", "status", "system-info", "list"):
        assert command in result.stdout


def test_run_help() -> None:
    result = runner.invoke(robocasa_app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--capability" in result.stdout
    assert "--output-uri" in result.stdout


def test_deploy_help() -> None:
    result = runner.invoke(robocasa_app, ["deploy", "--help"])
    assert result.exit_code == 0
    assert "--gpu-type" in result.stdout
    assert "--auth-mode" in result.stdout


def test_status_help() -> None:
    result = runner.invoke(robocasa_app, ["status", "--help"])
    assert result.exit_code == 0
    assert "--run-id" in result.stdout


def test_system_info_help() -> None:
    result = runner.invoke(robocasa_app, ["system-info", "--help"])
    assert result.exit_code == 0


def test_list_help() -> None:
    result = runner.invoke(robocasa_app, ["list", "--help"])
    assert result.exit_code == 0


def test_run_requires_capability() -> None:
    result = runner.invoke(robocasa_app, ["run"])
    assert result.exit_code != 0


def test_run_invalid_capability_local() -> None:
    result = runner.invoke(
        robocasa_app,
        ["run", "--capability", "bogus", "--output-uri", "s3://bucket/out"],
    )
    assert result.exit_code != 0


def test_system_info_local() -> None:
    result = runner.invoke(robocasa_app, ["system-info", "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"


def test_run_trajectory_export_help() -> None:
    result = runner.invoke(robocasa_app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--capability" in result.stdout


def test_run_trajectory_export_capability_accepted() -> None:
    # The schema accepts the new trajectory export capability.
    from npa.workbench.robocasa.schemas import RoboCasaRunRequest

    req = RoboCasaRunRequest(
        capability="kitchen_trajectory_export",
        output_uri="s3://bucket/out",
        iterations=5,
        num_envs=2,
    )
    assert req.capability == "kitchen_trajectory_export"
    assert req.iterations == 5
    assert req.num_envs == 2
