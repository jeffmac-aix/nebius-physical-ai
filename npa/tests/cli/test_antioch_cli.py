from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from npa.cli.main import app
from npa.cli.workbench.antioch import _deployment


def test_run_propagates_explicit_non_cartpole_metadata(monkeypatch) -> None:  # noqa: ANN001
    captured = {}

    def run(request, **kwargs):  # noqa: ANN001, ANN003, ANN202
        captured["request"] = request
        return {"status": "completed"}

    monkeypatch.setattr("npa.sdk.workbench.antioch.run", run)
    result = CliRunner().invoke(
        app,
        [
            "workbench",
            "antioch",
            "run",
            "--input-path",
            "s3://safe/input",
            "--output-path",
            "s3://safe/output",
            "--workflow-run",
            "run-1",
            "--state-id",
            "simulate",
            "--robot-type",
            "warehouse-arm",
            "--task",
            "Place the blue component in the inspection tray",
            "--suite",
            "stable-suite",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    request = captured["request"]
    assert request.robot_type == "warehouse-arm"
    assert request.task == "Place the blue component in the inspection tray"


def test_deployment_uses_terms_secret_and_workload_identity_storage() -> None:
    manifest = _deployment(
        "registry.invalid/npa-antioch:test",
        "npa-antioch",
        "workbench",
        "antioch-config",
        "service-token",
        "terms-acceptance",
    )
    container = manifest["items"][0]["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item for item in container["env"]}
    assert env["NPA_ANTIOCH_ACCEPT_TERMS"]["valueFrom"]["secretKeyRef"] == {
        "name": "terms-acceptance",
        "key": "accepted",
    }
    assert env["AWS_ENDPOINT_URL"]["value"].startswith("https://storage.")
    assert "envFrom" not in container
    rendered = json.dumps(manifest)
    assert "AWS_ACCESS_KEY_ID" not in rendered
    assert "AWS_SECRET_ACCESS_KEY" not in rendered


def test_antioch_image_contains_self_contained_storage_resolver() -> None:
    npa_root = Path(__file__).resolve().parents[2]
    dockerfile = (npa_root / "docker/workbench/antioch/Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "COPY src/npa/workbench/antioch" in dockerfile
    assert "storage_config import resolve_storage_client" in dockerfile
    assert "COPY src/npa/clients/config.py" not in dockerfile
