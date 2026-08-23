"""Self-contained S3 resolver for the minimal Antioch adapter image."""

from __future__ import annotations

import os

from npa.clients.storage import StorageClient


DEFAULT_NEBIUS_STORAGE_ENDPOINT = "https://storage.eu-north1.nebius.cloud"


def resolve_storage_client() -> StorageClient:
    """Resolve routing while leaving credentials to boto's workload-identity chain."""

    endpoint = (
        os.environ.get("AWS_ENDPOINT_URL", "").strip()
        or os.environ.get("NEBIUS_S3_ENDPOINT", "").strip()
        or os.environ.get("NPA_STORAGE_ENDPOINT", "").strip()
        or DEFAULT_NEBIUS_STORAGE_ENDPOINT
    )
    return StorageClient.from_environment(endpoint_url=endpoint)
