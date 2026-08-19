from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
import time

import pytest

from npa.workflows.byof import openpi_checkpoint_cache as cache


def _md5(value: bytes) -> str:
    digest = hashlib.md5(value, usedforsecurity=False).digest()
    return base64.b64encode(digest).decode("ascii")


@pytest.fixture
def fake_upstream(monkeypatch: pytest.MonkeyPatch):
    payloads = {"params/model": b"weights", "assets/norm": b"normalization"}
    records = [
        {
            "name": cache.OBJECT_PREFIX + name,
            "generation": str(index + 10),
            "size": len(value),
            "md5Hash": _md5(value),
            "crc32c": "test",
        }
        for index, (name, value) in enumerate(payloads.items())
    ]
    records.sort(key=lambda item: item["name"])
    revision = cache.manifest_sha256(records)
    monkeypatch.setattr(cache, "EXPECTED_MANIFEST_SHA256", revision)
    monkeypatch.setattr(cache, "EXPECTED_OBJECT_COUNT", len(records))
    monkeypatch.setattr(cache, "EXPECTED_TOTAL_SIZE", sum(map(len, payloads.values())))

    def read_json(_url: str):
        return {"items": records}

    def download(record, destination: Path):
        relative = str(record["name"]).removeprefix(cache.OBJECT_PREFIX)
        destination.write_bytes(payloads[relative])

    return records, payloads, read_json, download


def test_cold_population_and_verified_warm_readonly_reuse(
    tmp_path: Path, fake_upstream
) -> None:
    records, _, read_json, download = fake_upstream
    environ = {cache.OPENPI_TERMS_ENV: cache.OPENPI_TERMS_ACCEPTED_VALUE}
    path, populated = cache.populate_cache(
        tmp_path, environ=environ, read_json=read_json, download=download
    )
    assert populated is True
    assert path == cache.checkpoint_path(tmp_path)
    path.chmod(0o555)

    def refuse_download(_record, _destination):
        raise AssertionError("a verified warm cache must not redownload")

    reused, populated = cache.populate_cache(
        tmp_path, environ=environ, read_json=read_json, download=refuse_download
    )
    assert populated is False
    assert reused == path
    assert cache.verify_cache(tmp_path, records) == path


def test_concurrent_population_has_one_writer(tmp_path: Path, fake_upstream) -> None:
    _, _, read_json, base_download = fake_upstream
    environ = {cache.OPENPI_TERMS_ENV: cache.OPENPI_TERMS_ACCEPTED_VALUE}
    calls = 0
    calls_lock = threading.Lock()

    def counted_download(record, destination):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.02)
        base_download(record, destination)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: cache.populate_cache(
                    tmp_path,
                    environ=environ,
                    read_json=read_json,
                    download=counted_download,
                ),
                range(4),
            )
        )
    assert calls == 2
    assert sum(populated for _, populated in results) == 1
    assert len({path for path, _ in results}) == 1


def test_corrupt_and_partial_cache_refuses_then_recovers(
    tmp_path: Path, fake_upstream
) -> None:
    records, _, read_json, download = fake_upstream
    environ = {cache.OPENPI_TERMS_ENV: cache.OPENPI_TERMS_ACCEPTED_VALUE}
    path, _ = cache.populate_cache(
        tmp_path, environ=environ, read_json=read_json, download=download
    )
    first = path / cache._relative_path(records[0])
    first.write_bytes(b"corrupt")
    with pytest.raises(cache.OpenPICacheError, match="size mismatch|checksum mismatch"):
        cache.verify_cache(tmp_path, records)
    recovered, populated = cache.populate_cache(
        tmp_path, environ=environ, read_json=read_json, download=download
    )
    assert populated is True
    assert cache.verify_cache(tmp_path, records) == recovered

    (cache.cache_identity_root(tmp_path) / cache.READY_MARKER).unlink()
    with pytest.raises(cache.OpenPICacheError, match="ready marker"):
        cache.verify_cache(tmp_path, records)


def test_cache_miss_without_terms_fails_before_metadata_or_download(
    tmp_path: Path, fake_upstream
) -> None:
    touched = False

    def unexpected(_value):
        nonlocal touched
        touched = True
        raise AssertionError

    with pytest.raises(cache.OpenPICacheError, match="terms acceptance"):
        cache.populate_cache(
            tmp_path, environ={}, read_json=unexpected, download=unexpected
        )
    assert touched is False


def test_revision_identity_is_immutable_and_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = cache.cache_identity_root(tmp_path)
    monkeypatch.setattr(cache, "EXPECTED_MANIFEST_SHA256", "f" * 64)
    second = cache.cache_identity_root(tmp_path)
    assert first != second
    assert first.parent.parent == second.parent.parent


def test_ready_marker_contains_no_acceptance_or_credentials(
    tmp_path: Path, fake_upstream
) -> None:
    _, _, read_json, download = fake_upstream
    path, _ = cache.populate_cache(
        tmp_path,
        environ={cache.OPENPI_TERMS_ENV: cache.OPENPI_TERMS_ACCEPTED_VALUE},
        read_json=read_json,
        download=download,
    )
    marker = json.loads((path.parent / cache.READY_MARKER).read_text())
    assert marker["revision"] == cache.EXPECTED_MANIFEST_SHA256
    text = json.dumps(marker).lower()
    assert "token" not in text
    assert "accept" not in text
