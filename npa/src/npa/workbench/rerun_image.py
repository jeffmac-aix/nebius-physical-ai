"""Build, inspect, push, and verify the checked-in Rerun workflow image."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Mapping, Sequence

from npa.clients.config import list_projects
from npa.clients.nebius import get_registry_identity
from npa.clients.nebius_auth import strip_ambient_token_env
from npa.orchestration.skypilot.image_bootstrap_contract import (
    ATTESTATION_LABEL,
    CONTRACT_VERSION,
)

IMAGE_NAME = "npa-rerun-viewer"
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENTRYPOINT = ["/opt/npa/docker/workbench/rerun-viewer/entrypoint.sh"]
_PROBE = (
    "set -eu; "
    "for c in sh sudo sshd rsync service; do command -v \"$c\" >/dev/null; done; "
    "sudo -n true; test -w /tmp; test -n \"$HOME\"; test -w \"$HOME\"; "
    "printf compatible"
)


class RerunImageError(RuntimeError):
    """A fixed Rerun image operation failed closed."""


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise RerunImageError(f"required executable could not run: {argv[0]}") from exc


def _require_success(
    result: subprocess.CompletedProcess[str], *, operation: str
) -> str:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-1200:]
        raise RerunImageError(f"{operation} failed (exit {result.returncode}): {detail}")
    return (result.stdout or "").strip()


def _registry_token(profile: str) -> str:
    """Mint a fresh task-registry token without trusting ambient credentials."""

    argv = ["nebius"]
    if profile:
        argv.extend(["--profile", profile])
    argv.extend(["iam", "get-access-token"])
    try:
        result = subprocess.run(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=strip_ambient_token_env(),
        )
    except OSError as exc:
        raise RerunImageError("Nebius CLI could not mint a task registry token") from exc
    token = (result.stdout or "").strip()
    if result.returncode != 0 or not token:
        detail = (result.stderr or result.stdout or "no token returned").strip()[-1200:]
        raise RerunImageError(f"task registry token mint failed: {detail}")
    return token


def _project_registry(project: str) -> tuple[str, str]:
    alias = str(project or "").strip()
    stanza = list_projects().get(alias)
    if not isinstance(stanza, dict):
        raise RerunImageError("exact configured project is required")
    project_id = str(stanza.get("project_id") or "").strip()
    registry_id = str(stanza.get("registry_id") or "").strip()
    registry = str(stanza.get("container_registry") or "").strip().rstrip("/")
    if not project_id or not registry_id or not registry:
        raise RerunImageError("project has no NPA-verified task registry")
    identity = get_registry_identity(registry_id)
    if identity is None or identity.project_id != project_id:
        raise RerunImageError("configured registry is absent or belongs to another project")
    expected = (
        f"{identity.registry_fqdn}/{identity.registry_id.removeprefix('registry-')}"
        if identity.registry_fqdn
        else ""
    )
    if not expected or registry != expected:
        raise RerunImageError("configured registry reference fails exact identity binding")
    return registry, identity.profile


def _image_ref(project: str, tag: str) -> tuple[str, str]:
    exact_tag = str(tag or "").strip()
    if not _TAG_RE.fullmatch(exact_tag) or exact_tag in {"latest", "stable", "main"}:
        raise RerunImageError(
            "validation tag must be unique, immutable-looking, and at least 8 characters"
        )
    registry, profile = _project_registry(project)
    return f"{registry}/{IMAGE_NAME}:{exact_tag}", profile


def _repo_root(repo_root: Path | str) -> Path:
    root = Path(repo_root).resolve()
    dockerfile = root / "npa" / "docker" / "workbench" / "rerun-viewer" / "Dockerfile"
    entrypoint = root / "npa" / "docker" / "workbench" / "rerun-viewer" / "entrypoint.sh"
    if not dockerfile.is_file() or not entrypoint.is_file():
        raise RerunImageError("checked-in Rerun viewer build context is incomplete")
    return root


def _inspect_config(image: str) -> Mapping[str, Any]:
    raw = _require_success(
        _run(["docker", "image", "inspect", image]), operation="docker image inspect"
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RerunImageError("docker image inspect returned invalid JSON") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RerunImageError("docker image inspect returned an ambiguous result")
    return payload[0]


def _inspection(image: str) -> dict[str, Any]:
    item = _inspect_config(image)
    config = item.get("Config") if isinstance(item.get("Config"), dict) else {}
    labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
    image_id = str(item.get("Id") or "").strip()
    user = str(config.get("User") or "").strip()
    entrypoint = config.get("Entrypoint")
    if not _DIGEST_RE.fullmatch(image_id):
        raise RerunImageError("local image has no immutable sha256 config digest")
    if labels.get(ATTESTATION_LABEL) != CONTRACT_VERSION:
        raise RerunImageError("local image lacks the exact SkyPilot bootstrap attestation")
    if not user or user in {"0", "root"}:
        raise RerunImageError("local image does not declare a non-root runtime user")
    if entrypoint != _ENTRYPOINT:
        raise RerunImageError("local image entrypoint does not forward orchestrator argv")
    probe = _require_success(
        _run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "/bin/sh",
                image,
                "-lc",
                _PROBE,
            ]
        ),
        operation="Rerun image bootstrap capability probe",
    )
    if probe != "compatible":
        raise RerunImageError("Rerun image capability probe returned unexpected output")
    core = {
        "image": image,
        "image_id": image_id,
        "contract": CONTRACT_VERSION,
        "runtime_user": user,
        "entrypoint": list(entrypoint),
        "checks": [
            "non_root_user",
            "passwordless_sudo",
            "sshd",
            "rsync",
            "service",
            "writable_tmp",
            "writable_home",
            "argument_forwarding_entrypoint",
            "exact_oci_attestation",
        ],
    }
    core["inspection_digest"] = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return core


def build_rerun_viewer(
    *, project: str, tag: str, repo_root: Path | str
) -> dict[str, Any]:
    """Build only the checked-in Rerun viewer Dockerfile into the local engine."""

    if shutil.which("docker") is None:
        raise RerunImageError("docker is required to build the Rerun viewer image")
    image, _profile = _image_ref(project, tag)
    root = _repo_root(repo_root)
    result = _run(
        [
            "docker",
            "buildx",
            "build",
            "--platform",
            "linux/amd64",
            "--file",
            "docker/workbench/rerun-viewer/Dockerfile",
            "--tag",
            image,
            "--load",
            ".",
        ],
        cwd=root / "npa",
    )
    _require_success(result, operation="Rerun viewer build")
    inspected = _inspect_config(image)
    image_id = str(inspected.get("Id") or "").strip()
    if not _DIGEST_RE.fullmatch(image_id):
        raise RerunImageError("built image has no immutable sha256 config digest")
    return {"status": "built", "image": image, "image_id": image_id}


def inspect_rerun_viewer(*, project: str, tag: str) -> dict[str, Any]:
    """Probe the actual local built bytes and return digest-bound evidence."""

    image, _profile = _image_ref(project, tag)
    return {"status": "compatible", **_inspection(image)}


def push_rerun_viewer(
    *,
    project: str,
    tag: str,
    expected_image_id: str,
    inspection_digest: str,
) -> dict[str, Any]:
    """Push only bytes matching a prior exact local inspection."""

    image, profile = _image_ref(project, tag)
    evidence = _inspection(image)
    if evidence["image_id"] != str(expected_image_id or "").strip():
        raise RerunImageError("local image changed after the bound inspection")
    if evidence["inspection_digest"] != str(inspection_digest or "").strip():
        raise RerunImageError("inspection digest does not match the current image")
    registry_host = image.split("/", 1)[0]
    token = _registry_token(profile)
    login = _run(
        [
            "docker",
            "login",
            registry_host,
            "--username",
            "iam",
            "--password-stdin",
        ],
        input_text=token,
    )
    _require_success(login, operation="task registry login")
    pushed = _run(["docker", "push", image])
    _require_success(pushed, operation="Rerun image push")
    inspected = _inspect_config(image)
    repo_digests = inspected.get("RepoDigests")
    candidates = [
        str(item).partition("@")[2]
        for item in repo_digests
        if isinstance(item, str) and item.startswith(image.rsplit(":", 1)[0] + "@")
    ] if isinstance(repo_digests, list) else []
    output_digests = re.findall(
        r"\bdigest:\s*(sha256:[0-9a-f]{64})\b",
        f"{pushed.stdout or ''}\n{pushed.stderr or ''}",
        flags=re.IGNORECASE,
    )
    digests = sorted(
        {
            item.lower()
            for item in [*candidates, *output_digests]
            if _DIGEST_RE.fullmatch(item.lower())
        }
    )
    if len(digests) != 1:
        raise RerunImageError("push did not yield one immutable repository digest")
    digest = digests[0]
    return {
        "status": "pushed",
        "image": image,
        "image_id": evidence["image_id"],
        "inspection_digest": evidence["inspection_digest"],
        "digest": digest,
        "immutable_image": f"{image.rsplit(':', 1)[0]}@{digest}",
    }


def verify_rerun_viewer(
    *, project: str, tag: str, expected_digest: str
) -> dict[str, Any]:
    """Pull and re-probe the exact pushed digest, never the mutable tag."""

    image, _profile = _image_ref(project, tag)
    digest = str(expected_digest or "").strip()
    if not _DIGEST_RE.fullmatch(digest):
        raise RerunImageError("expected digest must be an immutable sha256 digest")
    immutable = f"{image.rsplit(':', 1)[0]}@{digest}"
    _require_success(_run(["docker", "pull", immutable]), operation="immutable image pull")
    evidence = _inspection(immutable)
    return {"status": "verified", "digest": digest, "immutable_image": immutable, **evidence}
