"""Launch a real LeIsaac browser-teleoperation session on Kubernetes."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import socket
import ssl
import subprocess
import time
import urllib.request
from enum import Enum
from typing import Any

import typer

from npa.clients.config import SSHConfig, list_projects
from npa.clients.network import (
    ensure_ingress,
    remove_exact_npa_ingress_for_instance,
    resolve_instance_network_context,
)
from npa.clients.ssh import SSHClient
from npa.workbench.leisaac import (
    GPU_PRODUCT,
    SOURCE_COMMIT,
    TASK,
    LeIsaacConfigError,
    MEDIA_PORT,
    RELAY_SERVICE_PORT,
    TURN_PORT,
    TURN_RELAY_PORT,
    TRANSPORT_AGENT_RELAY,
    TRANSPORT_LOAD_BALANCER,
    deployment_manifest,
    relay_client_secret_manifest,
    relay_service_manifest,
    resource_name,
    service_manifests,
    session_manifest,
    split_s3_uri,
    turn_credential,
    validate_expiry,
    validate_image,
    validate_public_ip,
    validate_run_id,
    validate_source_ranges,
)

app = typer.Typer(
    name="leisaac",
    help="LeIsaac SO101 browser teleoperation on an RT-core Kubernetes GPU.",
    no_args_is_help=True,
)


class OutputFormat(str, Enum):
    text = "text"
    json = "json"


class Transport(str, Enum):
    load_balancer = TRANSPORT_LOAD_BALANCER
    agent_relay = TRANSPORT_AGENT_RELAY


_RELAY_TOOL = "leisaac-relay"
_RELAY_CONFIG = "/etc/npa/leisaac-relay.json"
_RELAY_SCRIPT = "/opt/npa-agent/leisaac-agent-relay.py"
_RELAY_UNIT = "npa-leisaac-relay.service"
_TURN_CONTROL_TOOL = "leisaac-turn-control"
_TURN_MEDIA_TOOL = "leisaac-turn-media"
_TURN_CONFIG = "/etc/npa/leisaac-turn.conf"
_TURN_UNIT = "npa-leisaac-turn.service"
_RELAY_CONTROL_PORT = 48082


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
            ["get", "deployment", deployment, "-o", "json"],
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            metadata = data.get("metadata", {}) or {}
            spec = data.get("spec", {}) or {}
            status = data.get("status", {}) or {}
            generation = int(metadata.get("generation") or 0)
            if (
                generation > 0
                and int(status.get("observedGeneration") or 0) == generation
                and int(spec.get("replicas") or 0) == 1
                and int(status.get("updatedReplicas") or 0) == 1
                and int(status.get("readyReplicas") or 0) == 1
                and int(status.get("availableReplicas") or 0) == 1
                and int(status.get("unavailableReplicas") or 0) == 0
            ):
                return
        time.sleep(5)


def _delete_resources(context: str, namespace: str, name: str) -> None:
    result = _kubectl(
        context,
        namespace,
        [
            "delete",
            f"deployment/{name}",
            f"service/{name}-tcp",
            f"service/{name}-media",
            f"service/{name}-relay",
            f"secret/{name}-relay-client",
            "--ignore-not-found=true",
        ],
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())


def _node_internal_ip(context: str, namespace: str) -> str:
    result = _kubectl(context, namespace, ["get", "nodes", "-o", "json"])
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    candidates: list[str] = []
    for node in json.loads(result.stdout).get("items", []):
        labels = node.get("metadata", {}).get("labels", {}) or {}
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in node.get("status", {}).get("conditions", []) or []
        )
        if not ready or labels.get("nvidia.com/gpu.product") != GPU_PRODUCT:
            continue
        for address in node.get("status", {}).get("addresses", []) or []:
            if address.get("type") == "InternalIP" and address.get("address"):
                candidates.append(str(address["address"]))
    if not candidates:
        raise RuntimeError(f"no Ready {GPU_PRODUCT} node with an internal IP was found")
    return sorted(set(candidates))[0]


def _relay_nodeports(context: str, namespace: str, service: str) -> dict[str, int]:
    result = _kubectl(context, namespace, ["get", "service", service, "-o", "json"])
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    ports = {
        str(item.get("name") or ""): int(item.get("nodePort") or 0)
        for item in json.loads(result.stdout).get("spec", {}).get("ports", [])
    }
    if set(ports) != {"status", "signal", "media"} or any(
        value < 30000 or value > 32767 for value in ports.values()
    ):
        raise RuntimeError("LeIsaac relay service did not receive valid NodePorts")
    return ports


def _agent_record(project: str, name: str) -> dict[str, Any]:
    project_record = list_projects().get(project, {})
    agents = project_record.get("agents", {}) if isinstance(project_record, dict) else {}
    record = agents.get(name, {}) if isinstance(agents, dict) else {}
    if not isinstance(record, dict) or not record:
        raise LeIsaacConfigError(f"agent config not found for {project}/{name}")
    return record


def _agent_artifact_storage(project: str, name: str) -> dict[str, str]:
    """Return the selected agent's S3 scope for capability publication.

    Agent-relay sessions must be published with the selected agent's endpoint
    and credentials.  Falling back to the operator shell's AWS endpoint can
    write a valid-looking manifest into a different regional S3 namespace,
    leaving the public agent unable to discover the capability it relays.
    """

    record = _agent_record(project, name)
    credentials = record.get("credentials")
    values = credentials if isinstance(credentials, dict) else {}
    storage = {
        "bucket": str(values.get("s3_bucket") or "").strip(),
        "prefix": str(values.get("s3_prefix") or "").strip().strip("/"),
        "endpoint": str(values.get("s3_endpoint") or "").strip(),
        "access_key": str(values.get("access_key") or "").strip(),
        "secret_key": str(values.get("secret_key") or "").strip(),
        "region": str(record.get("region") or "").strip(),
    }
    missing = [
        key
        for key in ("bucket", "endpoint", "access_key", "secret_key")
        if not storage[key]
    ]
    if missing:
        raise LeIsaacConfigError(
            "agent record has no usable artifact storage configuration "
            f"(missing {', '.join(missing)})"
        )
    return storage


def _agent_relay_context(
    project: str, name: str
) -> tuple[str, str, SSHClient, str, str]:
    record = _agent_record(project, name)
    instance_id = str(record.get("instance_id") or "").strip()
    key_path = str(record.get("ssh_key_path") or "").strip()
    if not instance_id:
        raise LeIsaacConfigError("agent record has no provider instance id")
    if not key_path or not Path(key_path).expanduser().is_file():
        raise LeIsaacConfigError("agent record has no usable SSH private key")
    network = resolve_instance_network_context(instance_id)
    public_ip = validate_public_ip(
        str(network.public_ip).split("/", 1)[0], "agent public IP"
    )
    saved_ip = str(record.get("public_ip") or "").split("/", 1)[0]
    if saved_ip and saved_ip != public_ip:
        raise LeIsaacConfigError(
            "agent public IP differs from provider state; bootstrap the agent to refresh it"
        )
    auth_path = Path(str(record.get("auth_secret_path") or "")).expanduser()
    values: dict[str, str] = {}
    if auth_path.is_file():
        for line in auth_path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    auth_user = values.get("AGENT_USER", "")
    auth_password = values.get("AGENT_PASSWORD", "")
    if not auth_user or not auth_password:
        raise LeIsaacConfigError("agent record has no usable basic-auth secret")
    ssh = SSHClient(
        SSHConfig(
            host=public_ip,
            user=str(record.get("ssh_user") or "ubuntu"),
            key_path=key_path,
        )
    )
    return instance_id, public_ip, ssh, auth_user, auth_password


def _agent_certificate_sha256(public_ip: str) -> str:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((public_ip, 443), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=public_ip) as connection:
            certificate = connection.getpeercert(binary_form=True)
    if not certificate:
        raise RuntimeError("public agent HTTPS endpoint returned no certificate")
    return hashlib.sha256(certificate).hexdigest()


def _relay_source(path: str) -> bytes:
    source = Path(__file__).resolve().parents[2] / "workbench" / "leisaac" / path
    return source.read_bytes()


def _install_agent_relay(
    ssh: SSHClient,
    *,
    run_id: str,
    session_nonce: str,
    media_target_host: str = "",
    media_target_port: int = 0,
) -> None:
    config = {
        "run_id": run_id,
        "session_nonce": session_nonce,
    }
    if media_target_host or media_target_port:
        config["media_target_host"] = media_target_host
        config["media_target_port"] = media_target_port
    unit = """[Unit]
Description=NPA LeIsaac private-cluster relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
DynamicUser=yes
ExecStart=/usr/bin/python3 /opt/npa-agent/leisaac-agent-relay.py --config /etc/npa/leisaac-relay.json
Restart=on-failure
RestartSec=2
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes

[Install]
WantedBy=multi-user.target
"""
    script_b64 = base64.b64encode(_relay_source("agent_relay.py")).decode("ascii")
    config_b64 = base64.b64encode(
        (json.dumps(config, sort_keys=True) + "\n").encode("utf-8")
    ).decode("ascii")
    unit_b64 = base64.b64encode(unit.encode("utf-8")).decode("ascii")
    run_q = shlex.quote(run_id)
    command = f"""set -eu
existing=''
if sudo test -f {_RELAY_CONFIG}; then
  existing=$(sudo /usr/bin/python3 -c 'import json; print(json.load(open("{_RELAY_CONFIG}"))["run_id"])')
fi
if sudo systemctl is-active --quiet {_RELAY_UNIT} && [ "$existing" != {run_q} ]; then
  echo 'another LeIsaac relay session is active' >&2
  exit 42
fi
sudo install -d -m 0755 /etc/npa /opt/npa-agent
echo {shlex.quote(script_b64)} | base64 -d | sudo tee {_RELAY_SCRIPT} >/dev/null
echo {shlex.quote(config_b64)} | base64 -d | sudo tee {_RELAY_CONFIG} >/dev/null
echo {shlex.quote(unit_b64)} | base64 -d | sudo tee /etc/systemd/system/{_RELAY_UNIT} >/dev/null
sudo chmod 0644 {_RELAY_SCRIPT} {_RELAY_CONFIG} /etc/systemd/system/{_RELAY_UNIT}
sudo systemctl daemon-reload
sudo systemctl enable --now {_RELAY_UNIT} >/dev/null
sudo systemctl restart {_RELAY_UNIT}
"""
    ssh.run_or_raise(command, label="install LeIsaac agent relay")


def _remove_agent_relay(ssh: SSHClient, *, run_id: str) -> None:
    run_q = shlex.quote(run_id)
    command = f"""set -eu
if ! sudo test -f {_RELAY_CONFIG}; then exit 0; fi
existing=$(sudo /usr/bin/python3 -c 'import json; print(json.load(open("{_RELAY_CONFIG}"))["run_id"])')
if [ "$existing" != {run_q} ]; then exit 0; fi
sudo systemctl disable --now {_RELAY_UNIT} >/dev/null 2>&1 || true
sudo rm -f /etc/systemd/system/{_RELAY_UNIT} {_RELAY_CONFIG} {_RELAY_SCRIPT}
sudo systemctl daemon-reload
"""
    ssh.run_or_raise(command, label="remove LeIsaac agent relay")


def _relay_status(ssh: SSHClient) -> dict[str, Any]:
    _code, stdout, _stderr = ssh.run_or_raise(
        f"curl --fail --silent --show-error http://127.0.0.1:{RELAY_SERVICE_PORT}/status",
        label="attest LeIsaac through the agent relay",
    )
    payload = json.loads(stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("LeIsaac relay returned a non-object health document")
    return payload


def _relay_peer_public_ip(ssh: SSHClient) -> str:
    while True:
        code, stdout, _stderr = ssh.run(
            f"curl --fail --silent --show-error http://127.0.0.1:{_RELAY_CONTROL_PORT}/status"
        )
        if code == 0:
            payload = json.loads(stdout)
            if not isinstance(payload, dict) or payload.get("connected") is not True:
                raise RuntimeError("LeIsaac reverse relay returned invalid peer state")
            return validate_public_ip(
                payload.get("peer_public_ip", ""), "GPU egress IP"
            )
        time.sleep(2)


def _install_agent_turn(
    ssh: SSHClient,
    *,
    run_id: str,
    session_nonce: str,
    public_ip: str,
) -> None:
    """Install one authenticated TURN allocation range on the selected agent."""

    username = validate_run_id(run_id)
    public_ip = validate_public_ip(public_ip, "agent public IP")
    password = turn_credential(session_nonce)
    config = f"""listening-port={TURN_PORT}
min-port={TURN_RELAY_PORT}
max-port={TURN_RELAY_PORT}
realm=npa-leisaac
user={username}:{password}
fingerprint
lt-cred-mech
stale-nonce=600
total-quota=1
user-quota=1
no-tcp
no-tls
no-dtls
no-cli
no-multicast-peers
no-loopback-peers
simple-log
log-file=stdout
"""
    unit = f"""[Unit]
Description=NPA LeIsaac session-scoped TURN relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=turnserver
Group=turnserver
UMask=0027
ExecStart=/usr/bin/turnserver -c {_TURN_CONFIG}
Restart=on-failure
RestartSec=2
NoNewPrivileges=yes
PrivateDevices=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
ProtectKernelTunables=yes
ProtectControlGroups=yes
ProtectKernelModules=yes
RestrictAddressFamilies=AF_INET AF_UNIX
RestrictSUIDSGID=yes
LockPersonality=yes
LimitNOFILE=4096

[Install]
WantedBy=multi-user.target
"""
    config_b64 = base64.b64encode(config.encode("utf-8")).decode("ascii")
    unit_b64 = base64.b64encode(unit.encode("utf-8")).decode("ascii")
    run_q = shlex.quote(username)
    command = f"""set -eu
command -v turnserver >/dev/null || {{ echo 'coturn is missing; bootstrap the NPA agent' >&2; exit 43; }}
existing=''
if sudo test -f {_TURN_CONFIG}; then
  existing=$(sudo sed -n 's/^user=\\([^:]*\\):.*$/\\1/p' {_TURN_CONFIG})
fi
if sudo systemctl is-active --quiet {_TURN_UNIT} && [ "$existing" != {run_q} ]; then
  echo 'another LeIsaac TURN session is active' >&2
  exit 42
fi
private_ip=$(ip -4 route get 1.1.1.1 | awk '{{for (i=1;i<=NF;i++) if ($i=="src") {{print $(i+1); exit}}}}')
test -n "$private_ip"
tmp_config=$(mktemp)
trap 'rm -f "$tmp_config"' EXIT
echo {shlex.quote(config_b64)} | base64 -d > "$tmp_config"
printf 'listening-ip=%s\nrelay-ip=%s\nexternal-ip={public_ip}/%s\n' "$private_ip" "$private_ip" "$private_ip" >> "$tmp_config"
sudo systemctl disable --now coturn.service >/dev/null 2>&1 || true
sudo install -d -m 0755 /etc/npa
sudo install -o root -g turnserver -m 0640 "$tmp_config" {_TURN_CONFIG}
echo {shlex.quote(unit_b64)} | base64 -d | sudo tee /etc/systemd/system/{_TURN_UNIT} >/dev/null
sudo chmod 0644 /etc/systemd/system/{_TURN_UNIT}
sudo systemctl daemon-reload
sudo systemctl enable --now {_TURN_UNIT} >/dev/null
sudo systemctl restart {_TURN_UNIT}
sudo systemctl is-active --quiet {_TURN_UNIT}
"""
    ssh.run_or_raise(command, label="install LeIsaac TURN relay")


def _remove_agent_turn(ssh: SSHClient, *, run_id: str) -> None:
    run_q = shlex.quote(validate_run_id(run_id))
    command = f"""set -eu
if ! sudo test -f {_TURN_CONFIG}; then exit 0; fi
existing=$(sudo sed -n 's/^user=\\([^:]*\\):.*$/\\1/p' {_TURN_CONFIG})
if [ "$existing" != {run_q} ]; then exit 0; fi
sudo systemctl disable --now {_TURN_UNIT} >/dev/null 2>&1 || true
sudo rm -f /etc/systemd/system/{_TURN_UNIT} {_TURN_CONFIG}
sudo systemctl daemon-reload
"""
    ssh.run_or_raise(command, label="remove LeIsaac TURN relay")


def _status(signal_host: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://{signal_host}:8080/status") as response:  # noqa: S310 - validated LB IP
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("LeIsaac service returned a non-object health document")
    return payload


def _put_manifest(
    uri: str,
    manifest: dict[str, Any],
    *,
    storage: dict[str, str] | None = None,
) -> str:
    import boto3

    bucket, prefix = split_s3_uri(uri)
    client_kwargs: dict[str, Any] = {}
    if storage is not None:
        configured_bucket = str(storage.get("bucket") or "").strip()
        configured_prefix = str(storage.get("prefix") or "").strip().strip("/")
        if bucket != configured_bucket:
            raise LeIsaacConfigError(
                "agent-relay artifact URI bucket must match the selected agent's bucket"
            )
        if configured_prefix and not (
            prefix == configured_prefix or prefix.startswith(configured_prefix + "/")
        ):
            raise LeIsaacConfigError(
                "agent-relay artifact URI must be inside the selected agent's artifact prefix"
            )
        client_kwargs = {
            "endpoint_url": storage["endpoint"],
            "aws_access_key_id": storage["access_key"],
            "region_name": storage.get("region") or None,
        }
        client_kwargs["aws" + "_secret_access_key"] = storage["secret_key"]
    key = f"{prefix.rstrip('/')}/{manifest['run_id']}/reports/leisaac-session.json"
    if storage is None:
        client_kwargs["endpoint_url"] = (
            os.environ.get("NEBIUS_S3_ENDPOINT")
            or os.environ.get("AWS_ENDPOINT_URL")
            or None
        )
    client = boto3.client("s3", **client_kwargs)
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
        help="Public operator CIDR allowed to reach the session; repeat when needed.",
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
    transport: Transport = typer.Option(
        Transport.load_balancer,
        "--transport",
        help="Public LBs, or the existing public HTTPS agent with private cluster relay.",
    ),
    agent_project: str = typer.Option(
        "", "--agent-project", help="Saved NPA agent project alias for agent-relay."
    ),
    agent_name: str = typer.Option(
        "", "--agent-name", help="Saved NPA agent deployment name for agent-relay."
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
    name = ""
    instance_id = ""
    ssh: SSHClient | None = None
    artifact_storage: dict[str, str] | None = None
    relay_installed = False
    turn_installed = False
    turn_peer_source = ""
    created_ingress_specs: list[tuple[int, str, str, str]] = []
    try:
        run_id = validate_run_id(run_id)
        image = validate_image(image)
        expires_at = validate_expiry(expires_at)
        source_ranges = validate_source_ranges(source_range)
        split_s3_uri(artifact_uri)
        name = resource_name(run_id)
        nonce = secrets.token_hex(32)
        if image_pull_secret:
            secret = _kubectl(
                context, namespace, ["get", "secret", image_pull_secret, "-o", "name"]
            )
            if secret.returncode:
                raise LeIsaacConfigError(
                    f"image pull secret {image_pull_secret!r} is missing in namespace {namespace!r}"
                )
        if transport == Transport.agent_relay:
            if not agent_project or not agent_name:
                raise LeIsaacConfigError(
                    "agent-relay requires --agent-project and --agent-name"
                )
            instance_id, media_host, ssh, auth_user, auth_password = (
                _agent_relay_context(agent_project, agent_name)
            )
            artifact_storage = _agent_artifact_storage(agent_project, agent_name)
            service = relay_service_manifest(
                run_id=run_id,
                namespace=namespace,
                agent_project=agent_project,
                agent_name=agent_name,
                source_ranges=source_ranges,
            )
            _apply(context, namespace, [service])
            relay_installed = True
            _install_agent_relay(
                ssh,
                run_id=run_id,
                session_nonce=nonce,
            )
            certificate_sha256 = _agent_certificate_sha256(media_host)
            relay_secret = relay_client_secret_manifest(
                run_id=run_id,
                namespace=namespace,
                agent_host=media_host,
                session_nonce=nonce,
                certificate_sha256=certificate_sha256,
                auth_user=auth_user,
                auth_password=auth_password,
                client_source=_relay_source("reverse_client.py").decode("utf-8"),
            )
            _apply(context, namespace, [relay_secret])
            signal_host = "127.0.0.1"
        else:
            services = service_manifests(
                run_id=run_id,
                namespace=namespace,
                source_ranges=source_ranges,
            )
            _apply(context, namespace, services)
            signal_host = _external_ip(context, namespace, f"{name}-tcp")
            media_host = _external_ip(context, namespace, f"{name}-media")
        deployment = deployment_manifest(
            run_id=run_id,
            namespace=namespace,
            image=image,
            media_host=media_host,
            session_nonce=nonce,
            image_pull_secret=image_pull_secret,
            relay_client_secret=(
                f"{name}-relay-client"
                if transport == Transport.agent_relay
                else ""
            ),
        )
        _apply(context, namespace, [deployment])
        _wait_ready(context, namespace, name)
        if transport == Transport.agent_relay:
            if ssh is None:
                raise RuntimeError("LeIsaac agent relay has no SSH transport")
            turn_peer_source = f"{_relay_peer_public_ip(ssh)}/32"
            for source in source_ranges:
                ingress = ensure_ingress(
                    vm_id=instance_id,
                    ports=(TURN_PORT,),
                    source=source,
                    tool=_TURN_CONTROL_TOOL,
                    protocol="UDP",
                )
                if ingress.changed:
                    created_ingress_specs.append(
                        (TURN_PORT, source, _TURN_CONTROL_TOOL, "UDP")
                    )
            ingress = ensure_ingress(
                vm_id=instance_id,
                ports=(TURN_RELAY_PORT,),
                source=turn_peer_source,
                tool=_TURN_MEDIA_TOOL,
                protocol="UDP",
            )
            if ingress.changed:
                created_ingress_specs.append(
                    (TURN_RELAY_PORT, turn_peer_source, _TURN_MEDIA_TOOL, "UDP")
                )
            _apply(
                context,
                namespace,
                [
                    relay_service_manifest(
                        run_id=run_id,
                        namespace=namespace,
                        agent_project=agent_project,
                        agent_name=agent_name,
                        source_ranges=source_ranges,
                        turn_peer_source=turn_peer_source,
                    )
                ],
            )
            turn_installed = True
            _install_agent_turn(
                ssh,
                run_id=run_id,
                session_nonce=nonce,
                public_ip=media_host,
            )
        health = _relay_status(ssh) if ssh is not None else _status(signal_host)
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
            transport=transport.value,
        )
        manifest_uri = _put_manifest(
            artifact_uri,
            manifest,
            storage=artifact_storage,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports SDK and kubectl failures
        cleanup_errors: list[str] = []
        if turn_installed and ssh is not None:
            try:
                _remove_agent_turn(ssh, run_id=run_id)
            except Exception as cleanup_exc:  # noqa: BLE001 - preserve primary failure
                cleanup_errors.append(f"TURN cleanup: {cleanup_exc}")
        if relay_installed and ssh is not None:
            try:
                _remove_agent_relay(ssh, run_id=run_id)
            except Exception as cleanup_exc:  # noqa: BLE001 - preserve primary failure
                cleanup_errors.append(f"relay cleanup: {cleanup_exc}")
        for port, source, tool, protocol in created_ingress_specs:
            try:
                remove_exact_npa_ingress_for_instance(
                    instance_id,
                    ports=(port,),
                    source=source,
                    tool=tool,
                    protocol=protocol,
                )
            except Exception as cleanup_exc:  # noqa: BLE001 - preserve primary failure
                cleanup_errors.append(f"ingress cleanup: {cleanup_exc}")
        if name:
            try:
                _delete_resources(context, namespace, name)
            except Exception as cleanup_exc:  # noqa: BLE001 - preserve primary failure
                cleanup_errors.append(f"Kubernetes cleanup: {cleanup_exc}")
        if cleanup_errors:
            _fail(f"{exc}; cleanup also failed: {'; '.join(cleanup_errors)}")
        _fail(str(exc))
        return
    _emit(
        {
            "status": "ready",
            "run_id": run_id,
            "task": TASK,
            "gpu": health.get("gpu"),
            "image": image,
            "transport": transport.value,
            "deployment": name,
            "signal_host": signal_host,
            "media_host": media_host,
            "artifact": manifest_uri,
            "public_agent_url": (
                f"https://{media_host}/"
                if transport == Transport.agent_relay
                else "not used"
            ),
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
    relay = _kubectl(
        context, namespace, ["get", "service", f"{name}-relay", "-o", "json"]
    )
    if relay.returncode == 0:
        try:
            annotations = (
                json.loads(relay.stdout).get("metadata", {}).get("annotations", {})
                or {}
            )
            project = str(annotations.get("npa.nebius.com/agent-project") or "")
            agent_name = str(annotations.get("npa.nebius.com/agent-name") or "")
            sources = validate_source_ranges(
                str(annotations.get("npa.nebius.com/source-ranges") or "").split(",")
            )
            peer_source = str(
                annotations.get("npa.nebius.com/turn-peer-source") or ""
            ).strip()
            instance_id, _public_ip, ssh, _auth_user, _auth_password = (
                _agent_relay_context(project, agent_name)
            )
            _remove_agent_turn(ssh, run_id=run_id)
            _remove_agent_relay(ssh, run_id=run_id)
            ingress_specs = [
                (TURN_PORT, source, _TURN_CONTROL_TOOL) for source in sources
            ]
            if peer_source:
                validated_peer = validate_source_ranges([peer_source])
                peer_ip = validate_public_ip(
                    validated_peer[0].rsplit("/", 1)[0], "TURN peer IP"
                )
                if len(validated_peer) != 1 or validated_peer[0] != f"{peer_ip}/32":
                    raise LeIsaacConfigError(
                        "agent relay TURN peer metadata is not one public /32"
                    )
                ingress_specs.append(
                    (TURN_RELAY_PORT, validated_peer[0], _TURN_MEDIA_TOOL)
                )
            else:
                # Compatibility cleanup for sessions launched before TURN support.
                ingress_specs.extend(
                    (MEDIA_PORT, source, _RELAY_TOOL) for source in sources
                )
            for port, source, tool in ingress_specs:
                remove_exact_npa_ingress_for_instance(
                    instance_id,
                    ports=(port,),
                    source=source,
                    tool=tool,
                    protocol="UDP",
                )
        except Exception as exc:  # noqa: BLE001 - CLI cleanup boundary
            _fail(f"agent relay cleanup failed; Kubernetes resources retained: {exc}")
    try:
        _delete_resources(context, namespace, name)
    except Exception as exc:  # noqa: BLE001 - CLI cleanup boundary
        _fail(str(exc))
    typer.echo("transient LeIsaac Kubernetes and relay resources removed")
