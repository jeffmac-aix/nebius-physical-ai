"""Launch a real LeIsaac browser-teleoperation session on Kubernetes."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
import urllib.request
from enum import Enum
from typing import Any

import typer

from npa.workbench.leisaac import (
    GPU_PRODUCT,
    SOURCE_COMMIT,
    TASK,
    LeIsaacConfigError,
    deployment_manifest,
    resource_name,
    service_manifests,
    session_manifest,
    split_s3_uri,
    validate_expiry,
    validate_image,
    validate_run_id,
)

app = typer.Typer(
    name="leisaac",
    help="LeIsaac SO101 browser teleoperation on an RT-core Kubernetes GPU.",
    no_args_is_help=True,
)


class OutputFormat(str, Enum):
    text = "text"
    json = "json"


def _fail(message: str) -> None:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


def _kubectl(
    context: str, namespace: str, args: list[str], stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    command = ["kubectl"]
    if context:
        command.extend(["--context", context])
    command.extend(["--namespace", namespace, *args])
    return subprocess.run(
        command, input=stdin, capture_output=True, text=True, check=False
    )


def _apply(context: str, namespace: str, documents: list[dict[str, Any]]) -> None:
    payload = json.dumps({"apiVersion": "v1", "kind": "List", "items": documents})
    result = _kubectl(context, namespace, ["apply", "-f", "-"], stdin=payload)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())


def _external_ip(context: str, namespace: str, service: str) -> str:
    while True:
        result = _kubectl(
            context,
            namespace,
            [
                "get",
                "service",
                service,
                "-o",
                "jsonpath={.status.loadBalancer.ingress[0].ip}",
            ],
        )
        value = result.stdout.strip()
        if result.returncode == 0 and value:
            return value
        time.sleep(3)


def _wait_ready(context: str, namespace: str, deployment: str) -> None:
    while True:
        result = _kubectl(
            context,
            namespace,
            ["get", "deployment", deployment, "-o", "jsonpath={.status.readyReplicas}"],
        )
        if result.returncode == 0 and result.stdout.strip() == "1":
            return
        time.sleep(5)


def _status(signal_host: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://{signal_host}:8080/status") as response:  # noqa: S310 - validated LB IP
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("LeIsaac service returned a non-object health document")
    return payload


def _put_manifest(uri: str, manifest: dict[str, Any]) -> str:
    import boto3

    bucket, prefix = split_s3_uri(uri)
    key = f"{prefix.rstrip('/')}/{manifest['run_id']}/reports/leisaac-session.json"
    endpoint = (
        os.environ.get("NEBIUS_S3_ENDPOINT")
        or os.environ.get("AWS_ENDPOINT_URL")
        or None
    )
    client = boto3.client("s3", endpoint_url=endpoint)
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=(json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{bucket}/{key}"


def _emit(payload: dict[str, Any], output: OutputFormat) -> None:
    if output == OutputFormat.json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    for key, value in payload.items():
        typer.echo(f"{key}: {value}")


@app.command("launch")
def launch_cmd(
    run_id: str = typer.Option(
        ..., "--run-id", help="Run id used for artifact discovery and UI selection."
    ),
    image: str = typer.Option(
        ..., "--image", help="npa-leisaac image pinned as repository@sha256:digest."
    ),
    context: str = typer.Option(
        "", "--context", help="kubectl context for the RT-core GPU cluster."
    ),
    namespace: str = typer.Option(
        "default", "--namespace", help="Kubernetes namespace."
    ),
    source_range: list[str] = typer.Option(
        ...,
        "--source-range",
        help="Public CIDR allowed to reach status/signaling TCP ports; repeat for agent and operator.",
    ),
    artifact_uri: str = typer.Option(
        ..., "--artifact-uri", help="S3 prefix where the run manifest is written."
    ),
    expires_at: str = typer.Option(
        "",
        "--expires-at",
        help="Optional operator-chosen ISO-8601 expiry for UI discovery; omitted sessions remain live until destroyed.",
    ),
    image_pull_secret: str = typer.Option(
        "npa-registry", "--image-pull-secret", help="Existing registry pull secret."
    ),
    output: OutputFormat = typer.Option(
        OutputFormat.text, "--output", help="Output format."
    ),
) -> None:
    """Launch PickOrange with upstream keyboard teleoperation and publish its UI capability."""

    if (
        os.environ.get("OMNI_KIT_ACCEPT_EULA") != "YES"
        or os.environ.get("ISAACSIM_ACCEPT_EULA") != "YES"
    ):
        _fail(
            "set OMNI_KIT_ACCEPT_EULA=YES and ISAACSIM_ACCEPT_EULA=YES after accepting NVIDIA's EULAs"
        )
    try:
        run_id = validate_run_id(run_id)
        image = validate_image(image)
        expires_at = validate_expiry(expires_at)
        split_s3_uri(artifact_uri)
        services = service_manifests(
            run_id=run_id,
            namespace=namespace,
            source_ranges=source_range,
        )
        name = resource_name(run_id)
        if image_pull_secret:
            secret = _kubectl(
                context, namespace, ["get", "secret", image_pull_secret, "-o", "name"]
            )
            if secret.returncode:
                raise LeIsaacConfigError(
                    f"image pull secret {image_pull_secret!r} is missing in namespace {namespace!r}"
                )
        _apply(context, namespace, services)
        signal_host = _external_ip(context, namespace, f"{name}-tcp")
        media_host = _external_ip(context, namespace, f"{name}-media")
        nonce = secrets.token_hex(32)
        deployment = deployment_manifest(
            run_id=run_id,
            namespace=namespace,
            image=image,
            media_host=media_host,
            session_nonce=nonce,
            image_pull_secret=image_pull_secret,
        )
        _apply(context, namespace, [deployment])
        _wait_ready(context, namespace, name)
        health = _status(signal_host)
        if (
            health.get("state") != "ready"
            or health.get("task") != TASK
            or health.get("source_commit") != SOURCE_COMMIT
            or health.get("session_nonce") != nonce
        ):
            raise RuntimeError(f"LeIsaac live attestation failed: {health}")
        manifest = session_manifest(
            run_id=run_id,
            image=image,
            signal_host=signal_host,
            media_host=media_host,
            session_nonce=nonce,
            expires_at=expires_at,
            gpu=str(health.get("gpu") or GPU_PRODUCT),
            created_at=str(health.get("started_at") or "") or None,
        )
        manifest_uri = _put_manifest(artifact_uri, manifest)
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports SDK and kubectl failures
        _fail(str(exc))
        return
    _emit(
        {
            "status": "ready",
            "run_id": run_id,
            "task": TASK,
            "gpu": health.get("gpu"),
            "image": image,
            "deployment": name,
            "signal_host": signal_host,
            "media_host": media_host,
            "artifact": manifest_uri,
            "expires_at": expires_at or "none (service lifecycle)",
        },
        output,
    )


@app.command("status")
def status_cmd(
    run_id: str = typer.Option(..., "--run-id"),
    context: str = typer.Option("", "--context"),
    namespace: str = typer.Option("default", "--namespace"),
    output: OutputFormat = typer.Option(OutputFormat.text, "--output"),
) -> None:
    """Report the live Kubernetes objects for a LeIsaac run."""

    try:
        name = resource_name(validate_run_id(run_id))
    except LeIsaacConfigError as exc:
        _fail(str(exc))
        return
    result = _kubectl(
        context,
        namespace,
        [
            "get",
            "deployment,service,pod",
            "-l",
            f"app.kubernetes.io/instance={name}",
            "-o",
            "json",
        ],
    )
    if result.returncode:
        _fail((result.stderr or result.stdout).strip())
    data = json.loads(result.stdout)
    _emit({"run_id": run_id, "resources": data.get("items", [])}, output)


@app.command("destroy")
def destroy_cmd(
    run_id: str = typer.Option(..., "--run-id"),
    context: str = typer.Option("", "--context"),
    namespace: str = typer.Option("default", "--namespace"),
) -> None:
    """Delete this run's transient GPU deployment and LBs, preserving S3 evidence."""

    try:
        name = resource_name(validate_run_id(run_id))
    except LeIsaacConfigError as exc:
        _fail(str(exc))
        return
    result = _kubectl(
        context,
        namespace,
        [
            "delete",
            "deployment",
            name,
            "service",
            f"{name}-tcp",
            f"{name}-media",
            "--ignore-not-found=true",
        ],
    )
    if result.returncode:
        _fail((result.stderr or result.stdout).strip())
    typer.echo((result.stdout or "transient LeIsaac resources absent").strip())
