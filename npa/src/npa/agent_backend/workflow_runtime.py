"""Owner-scoped NPA workflow runtime lifecycle.

The public result deliberately describes NPA readiness rather than the
execution engine, local service, or credential-file details used internally.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Sequence

from npa.cli.invocation import internal_cli_argv
from npa.lifecycle_intent import OperationIntent, intent_boundary

RUNTIME_RESULT_SCHEMA = "npa.agent.workflow-runtime.v1"
_SCOPE_RE = re.compile(r"^[0-9a-f]{16,64}$")


class WorkflowRuntimeError(RuntimeError):
    """A workflow runtime operation failed with a model-safe diagnostic."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkflowRuntimeResult:
    """Typed, backend-neutral workflow runtime evidence."""

    status: str
    runtime_ready: bool
    target_ready: bool
    context_bound: bool
    reused: bool = False
    diagnostic_code: str = ""
    diagnostic: str = ""
    schema: str = RUNTIME_RESULT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _config_root() -> Path:
    return Path(
        os.environ.get("NPA_CONFIG_DIR", "").strip() or (Path.home() / ".npa")
    ).expanduser().resolve()


def _validated_scope(scope: str) -> str:
    value = str(scope or "").strip().lower()
    if not _SCOPE_RE.fullmatch(value):
        raise WorkflowRuntimeError(
            "invalid_scope",
            "workflow runtime scope must be a 16-64 character lowercase hex digest",
        )
    return value


def _runtime_paths(scope: str, cluster: str) -> tuple[Path, Path, int]:
    from npa.cluster.state import kubeconfig_file

    exact_scope = _validated_scope(scope)
    root = _config_root()
    state_dir = root / "workflow-runtimes" / exact_scope
    selected_access = kubeconfig_file(cluster)
    port_digest = hashlib.sha256(exact_scope.encode("utf-8")).hexdigest()
    port = 48_000 + int(port_digest[:4], 16) % 1_000
    return state_dir, selected_access, port


def _runtime_state_dir(scope: str) -> Path:
    return _config_root() / "workflow-runtimes" / _validated_scope(scope)


def workflow_runtime_environment(*, scope: str, cluster: str) -> dict[str, str]:
    """Return internal environment bindings for one exact workflow runtime."""

    state_dir, selected_access, port = _runtime_paths(scope, cluster)
    root = _config_root()
    return {
        "NPA_SKYPILOT_BIN": str(root / "skypilot-venv" / "bin" / "sky"),
        "NPA_SKYPILOT_ISOLATED_CONFIG_DIR": str(state_dir),
        "SKYPILOT_API_SERVER_ENDPOINT": f"http://127.0.0.1:{port}",
        "KUBECONFIG": str(selected_access),
    }


def _run_npa(
    args: Sequence[str], *, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        internal_cli_argv(args),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=dict(env) if env is not None else None,
    )


def _runtime_record_path(state_dir: Path) -> Path:
    return state_dir / "runtime.json"


def _write_runtime_record(
    state_dir: Path,
    *,
    project: str,
    cluster: str,
    scope: str,
    target_verified: bool,
) -> None:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _runtime_record_path(state_dir)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "schema": RUNTIME_RESULT_SCHEMA,
                "project": project,
                "cluster": cluster,
                "scope": scope,
                "target_verified": target_verified,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _load_runtime_record(state_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads(_runtime_record_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict) or value.get("schema") != RUNTIME_RESULT_SCHEMA:
        return {}
    return {str(key): item for key, item in value.items()}


def _generic_failure(code: str) -> WorkflowRuntimeError:
    messages = {
        "target_state_missing": "The selected NPA workflow target has no durable local identity.",
        "target_identity_mismatch": "The selected workflow target does not belong to the configured project.",
        "target_access_refresh_failed": "NPA could not refresh access for the selected workflow target.",
        "runtime_install_failed": "NPA could not prepare the local workflow execution runtime.",
        "runtime_service_failed": "NPA could not prepare the isolated workflow runtime service.",
        "target_verification_failed": "The selected workflow target did not pass NPA runtime verification.",
        "runtime_owner_record_failed": "NPA could not persist the workflow runtime owner record.",
        "runtime_stop_failed": "NPA could not stop the exact workflow runtime.",
    }
    return WorkflowRuntimeError(code, messages[code])


@intent_boundary(OperationIntent.ENSURE_PRESENT)
def prepare_workflow_runtime(
    *, project: str, cluster: str, scope: str
) -> WorkflowRuntimeResult:
    """Prepare one isolated NPA workflow runtime for an exact target and scope."""

    from npa.clients.config import resolve_environment
    from npa.cluster.state import load_cluster_state
    from npa.cli.skypilot import bootstrap_skypilot
    from npa.orchestration.skypilot.api_server import (
        IsolatedApiServerError,
        ensure_isolated_api_server,
    )

    state_dir, selected_access, port = _runtime_paths(scope, cluster)
    target = load_cluster_state(cluster)
    if target is None or not str(getattr(target, "provider_name", "") or "").strip():
        raise _generic_failure("target_state_missing")
    try:
        configured = resolve_environment(project)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _generic_failure("target_identity_mismatch") from exc
    configured_project = str(getattr(configured, "project_id", "") or "").strip()
    target_project = str(getattr(target, "project_id", "") or "").strip()
    if not configured_project or configured_project != target_project:
        raise _generic_failure("target_identity_mismatch")

    try:
        refreshed = _run_npa(
            (
                "cluster",
                "kubeconfig",
                "--cluster-name",
                str(target.provider_name),
                "--project",
                project,
                "--context",
                cluster,
                "--kubeconfig",
                str(selected_access),
            )
        )
    except OSError as exc:
        raise _generic_failure("target_access_refresh_failed") from exc
    if refreshed.returncode != 0 or not selected_access.is_file():
        raise _generic_failure("target_access_refresh_failed")

    try:
        runtime = bootstrap_skypilot()
    except Exception as exc:  # noqa: BLE001 - expose only a stable diagnostic
        raise _generic_failure("runtime_install_failed") from exc
    try:
        service = ensure_isolated_api_server(
            sky_bin=runtime.sky_bin,
            state_dir=state_dir,
            port=port,
            kubeconfig=selected_access,
        )
    except (IsolatedApiServerError, OSError, RuntimeError) as exc:
        raise _generic_failure("runtime_service_failed") from exc

    try:
        _write_runtime_record(
            state_dir,
            project=project,
            cluster=cluster,
            scope=_validated_scope(scope),
            target_verified=False,
        )
    except OSError as exc:
        raise _generic_failure("runtime_owner_record_failed") from exc

    env = {**os.environ, **workflow_runtime_environment(scope=scope, cluster=cluster)}
    try:
        verified = _run_npa(
            (
                "skypilot",
                "verify",
                "--cluster",
                cluster,
                "--output-format",
                "json",
            ),
            env=env,
        )
    except OSError as exc:
        raise _generic_failure("target_verification_failed") from exc
    if verified.returncode != 0:
        raise _generic_failure("target_verification_failed")
    try:
        _write_runtime_record(
            state_dir,
            project=project,
            cluster=cluster,
            scope=_validated_scope(scope),
            target_verified=True,
        )
    except OSError as exc:
        raise _generic_failure("runtime_owner_record_failed") from exc
    return WorkflowRuntimeResult(
        status="ready",
        runtime_ready=True,
        target_ready=True,
        context_bound=True,
        reused=bool(runtime.reused and service.reused),
    )


@intent_boundary(OperationIntent.OBSERVE)
def workflow_runtime_status(*, cluster: str, scope: str) -> WorkflowRuntimeResult:
    """Inspect one exact runtime without starting, repairing, or stopping it."""

    from npa.cli.skypilot import inspect_venv

    state_dir, selected_access, port = _runtime_paths(scope, cluster)
    runtime = inspect_venv(_config_root() / "skypilot-venv")
    record_path = state_dir / "server.json"
    owner = _load_runtime_record(state_dir)
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        record = {}
    bound = False
    owner_matches = (
        owner.get("cluster") == cluster
        and owner.get("scope") == scope
        and owner.get("target_verified") is True
    )
    if selected_access.is_file() and isinstance(record, dict) and owner_matches:
        expected = hashlib.sha256(selected_access.read_bytes()).hexdigest()
        bound = record.get("kubeconfig_sha256") == expected
    healthy = False
    if record.get("port") == port and bound:
        from npa.orchestration.skypilot.api_server import _healthy

        healthy = _healthy(f"http://127.0.0.1:{port}")
    ready = bool(runtime.installed and runtime.kubernetes_compatible and healthy)
    return WorkflowRuntimeResult(
        status="ready" if ready else "not_ready",
        runtime_ready=bool(runtime.installed and runtime.kubernetes_compatible and healthy),
        target_ready=bool(selected_access.is_file()),
        context_bound=bound,
        reused=ready,
        diagnostic_code="" if ready else "runtime_not_ready",
        diagnostic="" if ready else "Run the NPA workflow-runtime prepare operation.",
    )


@intent_boundary(OperationIntent.DESTROY)
def stop_workflow_runtime(*, cluster: str, scope: str) -> WorkflowRuntimeResult:
    """Stop only the exact isolated runtime service for this scope."""

    state_dir = _runtime_state_dir(scope)
    from npa.orchestration.skypilot.api_server import stop_isolated_api_server

    owner = _load_runtime_record(state_dir)
    if not owner:
        return WorkflowRuntimeResult(
            status="absent",
            runtime_ready=False,
            target_ready=False,
            context_bound=False,
        )
    if owner.get("cluster") != cluster or owner.get("scope") != scope:
        raise WorkflowRuntimeError(
            "runtime_owner_mismatch",
            "The requested workflow target does not match this runtime owner record.",
        )
    try:
        result = stop_isolated_api_server(state_dir=state_dir)
    except (OSError, RuntimeError) as exc:
        raise _generic_failure("runtime_stop_failed") from exc
    if result.get("status") in {"stopped", "absent"}:
        _runtime_record_path(state_dir).unlink(missing_ok=True)
    stopped = result.get("status") in {"stopped", "absent"}
    return WorkflowRuntimeResult(
        status=str(result.get("status") or "unknown"),
        runtime_ready=False,
        target_ready=False,
        context_bound=False,
        reused=False,
        diagnostic_code="" if stopped else "runtime_stop_failed",
    )


__all__ = [
    "RUNTIME_RESULT_SCHEMA",
    "WorkflowRuntimeError",
    "WorkflowRuntimeResult",
    "prepare_workflow_runtime",
    "stop_workflow_runtime",
    "workflow_runtime_environment",
    "workflow_runtime_status",
]
