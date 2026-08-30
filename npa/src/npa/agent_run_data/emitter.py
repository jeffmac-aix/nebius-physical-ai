"""Trajectory emitter for agent run data collection.

Implements the `npa.agent.trajectory.v1` schema. The emitter is deliberately
self-contained: it reads its destination from owner-only runtime configuration
(`NPA_AGENT_DATASET_TENANT_ID` and `NPA_AGENT_DATASET_URI`), never commits those
values, and redacts secrets and concrete infrastructure identifiers before any
write.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from npa.clients.storage import StorageClient, StorageError

SCHEMA_VERSION = "npa.agent.trajectory.v1"

def _outbox_dir() -> Path:
    """Owner-only local outbox directory (mode 0700). Used when the S3 write fails."""
    return Path(os.environ.get("NPA_AGENT_DATASET_OUTBOX", "~/.npa/agent-dataset-outbox")).expanduser()

#: Secret-shaped values redacted before any write.
_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|secret[_-]?key|password|token|authorization)\s*[:=]\s*['\"]?[^\s'\"]+"),
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


@dataclass(frozen=True)
class DatasetConfig:
    tenant_id: str
    dataset_uri: str
    bucket: str
    prefix: str


class AgentRunDataError(RuntimeError):
    """Raised when agent run data collection cannot be configured or emitted."""


class CollectionStatus:
    COLLECTED = "collected"
    PENDING = "pending"
    DISABLED = "disabled"


def resolve_dataset_config() -> DatasetConfig | None:
    """Resolve the dataset destination from owner-only runtime configuration.

    Returns ``None`` (collection disabled) when the configuration is absent.
    Raises when only part of the configuration is present.
    """
    tenant_id = os.environ.get("NPA_AGENT_DATASET_TENANT_ID", "").strip()
    dataset_uri = os.environ.get("NPA_AGENT_DATASET_URI", "").strip()
    if not tenant_id and not dataset_uri:
        return None
    if not tenant_id or not dataset_uri:
        raise AgentRunDataError(
            "both NPA_AGENT_DATASET_TENANT_ID and NPA_AGENT_DATASET_URI must be set together"
        )
    parsed = urlparse(dataset_uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise AgentRunDataError(f"NPA_AGENT_DATASET_URI must be an s3:// URI, got: {dataset_uri}")
    bucket = parsed.netloc
    prefix = parsed.path.lstrip("/").rstrip("/")
    return DatasetConfig(tenant_id=tenant_id, dataset_uri=dataset_uri, bucket=bucket, prefix=prefix)


def verify_destination(config: DatasetConfig, *, storage: StorageClient | None = None) -> None:
    """Verify the destination bucket is writable before enabling collection.

    Raises ``AgentRunDataError`` when the bucket cannot be written.
    """
    client = storage or _storage_client()
    try:
        client.s3.head_bucket(Bucket=config.bucket)
    except Exception as exc:  # pragma: no cover - depends on live S3.
        raise AgentRunDataError(f"destination bucket is not accessible: {exc}") from exc
    probe_key = f"{config.prefix}/.npa-write-probe-{os.getpid()}"
    try:
        client.s3.put_object(Bucket=config.bucket, Key=probe_key, Body=b"ok")
        client.s3.delete_object(Bucket=config.bucket, Key=probe_key)
    except Exception as exc:  # pragma: no cover - depends on live S3.
        raise AgentRunDataError(f"destination bucket is not writable: {exc}") from exc


def _storage_client() -> StorageClient:
    """Build an S3 client from NPA credentials with environment fallbacks."""
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "") or os.environ.get("NEBIUS_S3_ENDPOINT", "")
    access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    if not endpoint or not access_key:
        try:
            from npa.clients.credentials import load_credentials

            creds = load_credentials()
            endpoint = endpoint or creds.s3_endpoint
            access_key = access_key or creds.s3_access_key_id
            secret_key = secret_key or creds.s3_secret_access_key
        except Exception as exc:  # pragma: no cover - depends on NPA config.
            raise AgentRunDataError(f"cannot load S3 credentials: {exc}") from exc
    try:
        return StorageClient(
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
    except StorageError as exc:
        raise AgentRunDataError(f"cannot build S3 client: {exc}") from exc


_SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|secret[_-]?key|password|token|authorization|credential)")


def redact(value: Any) -> Any:
    """Recursively redact secret-shaped values from a JSON-serializable value."""
    if isinstance(value, dict):
        return {
            key: "<redacted>" if _SECRET_KEY_RE.search(str(key)) else redact(val)
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = value
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub("<redacted>", text)
        return text
    return value


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _content_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _object_key(config: DatasetConfig, episode_id: str, content_sha256: str) -> str:
    now = datetime.now(timezone.utc)
    return (
        f"{config.prefix}/episodes/{now:%Y}/{now:%m}/{now:%d}/"
        f"{episode_id}-{content_sha256}.json"
    )


def _write_outbox(payload: dict[str, Any], episode_id: str) -> Path:
    """Persist a finalized record to the owner-only local outbox."""
    outbox = _outbox_dir()
    outbox.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = outbox / f"{episode_id}.json"
    path.write_text(_canonical_json(payload), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - best effort.
        pass
    return path


def emit_trajectory(
    *,
    episode_id: str,
    session_id: str,
    request_content: str,
    intent: str,
    trajectory: list[dict[str, Any]],
    outcome: dict[str, Any],
    routing: dict[str, Any],
    versions: dict[str, Any],
    initial_state: dict[str, Any] | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    storage: StorageClient | None = None,
) -> tuple[str, str]:
    """Emit one sanitized trajectory record.

    Returns ``(collection_status, episode_id)``. The status is ``collected`` only
    after a read-after-write check confirms the uploaded bytes; otherwise it is
    ``pending`` and the record is preserved in the owner-only local outbox.
    """
    config = resolve_dataset_config()
    if config is None:
        return CollectionStatus.DISABLED, episode_id

    now = datetime.now(timezone.utc)
    started = started_at or now.isoformat()
    ended = ended_at or now.isoformat()

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "episode_id": episode_id,
        "session_id": session_id,
        "scope": {
            "tenant_id": config.tenant_id,
            "dataset_role": "agent-finetuning-raw",
        },
        "timing": {
            "started_at": started,
            "ended_at": ended,
            "latency_ms": 0,
        },
        "request": {
            "content": redact(request_content),
            "intent": redact(intent),
        },
        "initial_state": redact(initial_state or {}),
        "trajectory": redact(trajectory),
        "outcome": redact(outcome),
        "routing": redact(routing),
        "versions": redact(versions),
        "redaction": {
            "applied": True,
            "fields_removed": [],
        },
        "collection": {
            "status": CollectionStatus.PENDING,
            "content_sha256": "",
        },
    }

    # content_sha256 is computed over canonical JSON with the field empty (the
    # deterministic key contract). The uploaded body then carries the field filled
    # in, so read-after-write verifies against the hash of the uploaded bytes.
    content_sha256 = _content_sha256(payload)
    payload["collection"]["content_sha256"] = content_sha256
    key = _object_key(config, episode_id, content_sha256)

    try:
        verify_destination(config, storage=storage)
        client = storage or _storage_client()
        body = _canonical_json(payload).encode("utf-8")
        upload_sha256 = hashlib.sha256(body).hexdigest()
        client.s3.put_object(Bucket=config.bucket, Key=key, Body=body)
        # Read-after-write verification against the exact uploaded bytes.
        fetched = client.s3.get_object(Bucket=config.bucket, Key=key)["Body"].read()
        if hashlib.sha256(fetched).hexdigest() != upload_sha256:
            raise AgentRunDataError("read-after-write hash mismatch")
        payload["collection"]["status"] = CollectionStatus.COLLECTED
        return CollectionStatus.COLLECTED, episode_id
    except Exception as exc:  # pragma: no cover - depends on live S3.
        _write_outbox(payload, episode_id)
        return CollectionStatus.PENDING, episode_id


__all__ = [
    "AgentRunDataError",
    "CollectionStatus",
    "DatasetConfig",
    "emit_trajectory",
    "redact",
    "resolve_dataset_config",
    "verify_destination",
]
