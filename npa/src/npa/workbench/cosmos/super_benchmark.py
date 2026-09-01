"""Reproducible single-node B200 serving benchmark for Cosmos3-Super.

The benchmark runs inside the immutable public vLLM-Omni Cosmos3 image.  It
starts independent loopback services on disjoint GPU sets, validates one warmup
per service, and then measures a fixed production cell.  Only the prompt hashes
are recorded; prompt text remains in the operator's runtime model cache.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import math
import os
import shutil
import signal
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from npa.clients.storage import StorageClient

SCHEMA_VERSION = "npa.cosmos3-super.b200-benchmark.v1"
ATTEMPT_SCHEMA_VERSION = "npa.cosmos3-super.b200-attempt.v1"
MODEL_ID = "nvidia/Cosmos3-Super"
MODEL_REVISION = "e0262be9d8f7586bc24c069a2aed2b665bdff266"
IMAGE = (
    "docker.io/vllm/vllm-omni:cosmos3@"
    "sha256:6d2630c7d637b699557573f2c3fee8df5d4d0cd718977aa22549ed6a6ef30587"
)
PROMPT_ASSET = "assets/example_t2v_prompt.json"
NEGATIVE_PROMPT_ASSET = "assets/negative_prompt.json"
SEEDS = (17, 23, 41)
VIDEO_SECONDS = 189 / 24
SYNC_TIMEOUT_SECONDS = 5400
TOPOLOGY_ORDER = ("1x8", "2x4", "4x2", "8x1")
WORKLOAD = {
    "precision": "bf16",
    "size": "1280x720",
    "num_frames": 189,
    "fps": 24,
    "num_inference_steps": 35,
    "guidance_scale": 6.0,
    "flow_shift": 10.0,
    "max_sequence_length": 4096,
    "guardrails": False,
}


class Cosmos3SuperBenchmarkError(RuntimeError):
    """Raised when the fixed benchmark contract cannot be completed safely."""


@dataclass(frozen=True)
class Topology:
    name: str
    services: int
    gpus_per_service: int
    server_args: tuple[str, ...]


TOPOLOGIES: dict[str, Topology] = {
    "1x8": Topology(
        "1x8",
        1,
        8,
        (
            "--cfg-parallel-size",
            "2",
            "--ulysses-degree",
            "4",
            "--use-hsdp",
            "--hsdp-shard-size",
            "8",
        ),
    ),
    "2x4": Topology("2x4", 2, 4, ("--tensor-parallel-size", "4")),
    "4x2": Topology("4x2", 4, 2, ("--tensor-parallel-size", "2")),
    "8x1": Topology("8x1", 8, 1, ("--tensor-parallel-size", "1")),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256(value.encode("utf-8"))


def parse_topologies(value: str | Sequence[str]) -> tuple[str, ...]:
    raw = value.split(",") if isinstance(value, str) else value
    selected = tuple(str(item).strip() for item in raw if str(item).strip())
    if not selected:
        raise Cosmos3SuperBenchmarkError("at least one topology is required")
    unknown = [item for item in selected if item not in TOPOLOGIES]
    if unknown:
        raise Cosmos3SuperBenchmarkError(
            f"unknown topology {unknown[0]!r}; choose from {', '.join(TOPOLOGY_ORDER)}"
        )
    if len(set(selected)) != len(selected):
        raise Cosmos3SuperBenchmarkError("topologies must not contain duplicates")
    return selected


def benchmark_plan(
    *, output_path: str, topologies: str | Sequence[str], attempts: int = 24
) -> dict[str, Any]:
    selected = parse_topologies(topologies)
    if attempts < 1:
        raise Cosmos3SuperBenchmarkError("attempts must be positive")
    for name in selected:
        if attempts % TOPOLOGIES[name].services:
            raise Cosmos3SuperBenchmarkError(
                f"attempts={attempts} must divide evenly across {name}'s "
                f"{TOPOLOGIES[name].services} services"
            )
    if not output_path.startswith("s3://") and not Path(output_path).is_absolute():
        raise Cosmos3SuperBenchmarkError(
            "output_path must be an s3:// URI or absolute local directory"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "planned",
        "model": {"id": MODEL_ID, "revision": MODEL_REVISION},
        "runtime_image": IMAGE,
        "gpu": {"family": "B200", "node_gpu_count": 8},
        "topologies": [
            {
                "name": name,
                "services": TOPOLOGIES[name].services,
                "gpus_per_service": TOPOLOGIES[name].gpus_per_service,
                "server_parallelism": list(TOPOLOGIES[name].server_args),
                "request_concurrency_per_service": 1,
                "warmups_per_service": 1,
                "measured_attempts": attempts,
            }
            for name in selected
        ],
        "workload": dict(WORKLOAD),
        "seeds": list(SEEDS),
        "sync_timeout_seconds": SYNC_TIMEOUT_SECONDS,
        "output_path": output_path,
    }


def _visible_gpu_count() -> int:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise Cosmos3SuperBenchmarkError(
            "nvidia-smi failed; the benchmark requires one visible eight-GPU B200 node"
        )
    return len([line for line in result.stdout.splitlines() if line.strip()])


def _gpu_name() -> str:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
        check=False,
        capture_output=True,
        text=True,
    )
    names = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return next(iter(names)) if result.returncode == 0 and len(names) == 1 else ""


def _require_b200() -> dict[str, Any]:
    count = _visible_gpu_count()
    name = _gpu_name()
    if count != 8:
        raise Cosmos3SuperBenchmarkError(
            f"the benchmark requires exactly 8 visible GPUs; found {count}"
        )
    if "B200" not in name.upper():
        raise Cosmos3SuperBenchmarkError(
            f"the benchmark requires B200 GPUs; nvidia-smi reported {name or 'unknown'}"
        )
    return {"family": "B200", "node_gpu_count": count}


def _load_anchor_prompts() -> tuple[str, str, dict[str, str]]:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise Cosmos3SuperBenchmarkError(
            "huggingface_hub is required to resolve the pinned model prompt assets"
        ) from exc
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    root = Path(
        snapshot_download(
            MODEL_ID,
            revision=MODEL_REVISION,
            token=token,
            allow_patterns=[PROMPT_ASSET, NEGATIVE_PROMPT_ASSET],
        )
    )
    prompt = (root / PROMPT_ASSET).read_text(encoding="utf-8")
    negative = (root / NEGATIVE_PROMPT_ASSET).read_text(encoding="utf-8")
    if not prompt.strip() or not negative.strip():
        raise Cosmos3SuperBenchmarkError("pinned model prompt assets must be non-empty")
    return prompt, negative, {
        "prompt_sha256": _sha256_text(prompt),
        "negative_prompt_sha256": _sha256_text(negative),
    }


def service_command(topology: Topology, *, port: int) -> list[str]:
    return [
        "vllm",
        "serve",
        MODEL_ID,
        "--revision",
        MODEL_REVISION,
        "--omni",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--init-timeout",
        "1800",
        *topology.server_args,
        "--no-guardrails",
    ]


def _gpu_set(topology: Topology, replica: int) -> str:
    first = replica * topology.gpus_per_service
    return ",".join(str(index) for index in range(first, first + topology.gpus_per_service))


def _ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


@contextmanager
def running_services(
    topology: Topology, *, base_port: int, work_dir: Path
) -> Iterator[list[str]]:
    processes: list[subprocess.Popen[bytes]] = []
    logs: list[Any] = []
    urls: list[str] = []
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        for replica in range(topology.services):
            port = base_port + replica
            log = (work_dir / f"service-r{replica}.log").open("wb")
            logs.append(log)
            env = dict(os.environ)
            env["CUDA_VISIBLE_DEVICES"] = _gpu_set(topology, replica)
            env["VLLM_OMNI_VIDEO_SYNC_TIMEOUT"] = str(SYNC_TIMEOUT_SECONDS)
            process = subprocess.Popen(
                service_command(topology, port=port),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            processes.append(process)
            ready_url = f"http://127.0.0.1:{port}/v1/models"
            while not _ready(ready_url):
                if process.poll() is not None:
                    raise Cosmos3SuperBenchmarkError(
                        f"{topology.name} replica {replica} exited before readiness; "
                        "inspect the access-controlled workflow logs"
                    )
                time.sleep(5)
            urls.append(f"http://127.0.0.1:{port}/v1/videos/sync")
        yield urls
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=120)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        for log in logs:
            log.close()


def _run(command: list[str], *, binary: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=False, capture_output=True, text=not binary)


def validate_video(path: Path) -> dict[str, Any]:
    """Apply the full decode, shape, blank-frame, and basic-motion gate."""

    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        return {"valid": False, "errors": [{"check": "dependency", "detail": missing}]}
    errors: list[dict[str, Any]] = []
    decoded = _run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"]
    )
    if decoded.returncode != 0 or decoded.stderr.strip():
        errors.append(
            {"check": "decode", "detail": decoded.stderr.strip() or "nonzero exit"}
        )
    probed = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=width,height,avg_frame_rate,nb_read_frames,duration",
            "-of",
            "json",
            str(path),
        ]
    )
    stream: dict[str, Any] = {}
    if probed.returncode:
        errors.append({"check": "ffprobe", "detail": probed.stderr.strip()})
    else:
        try:
            stream = (json.loads(probed.stdout).get("streams") or [{}])[0]
        except (json.JSONDecodeError, IndexError) as exc:
            errors.append({"check": "ffprobe", "detail": str(exc)})
    if stream:
        if (stream.get("width"), stream.get("height")) != (1280, 720):
            errors.append(
                {"check": "geometry", "detail": f"{stream.get('width')}x{stream.get('height')}"}
            )
        try:
            fps = float(Fraction(stream["avg_frame_rate"]))
            if not math.isclose(fps, 24.0, abs_tol=0.001):
                errors.append({"check": "fps", "detail": fps})
        except (KeyError, ValueError, ZeroDivisionError) as exc:
            errors.append({"check": "fps", "detail": str(exc)})
        try:
            frames = int(stream["nb_read_frames"])
            if frames != 189:
                errors.append({"check": "frames", "detail": frames})
        except (KeyError, TypeError, ValueError) as exc:
            errors.append({"check": "frames", "detail": str(exc)})
        try:
            duration = float(stream["duration"])
            if not 7.80 <= duration <= 7.95:
                errors.append({"check": "duration", "detail": duration})
        except (KeyError, TypeError, ValueError) as exc:
            errors.append({"check": "duration", "detail": str(exc)})
    else:
        errors.append({"check": "stream", "detail": "no video stream"})

    sampled = _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            "select='eq(n,0)+eq(n,47)+eq(n,94)+eq(n,141)+eq(n,188)',scale=32:32,format=gray",
            "-vsync",
            "0",
            "-f",
            "rawvideo",
            "-",
        ],
        binary=True,
    )
    sample_size = 32 * 32
    samples: list[dict[str, float]] = []
    if sampled.returncode or len(sampled.stdout) != 5 * sample_size:
        errors.append(
            {
                "check": "sampling",
                "detail": f"expected {5 * sample_size} bytes, got {len(sampled.stdout)}",
            }
        )
    else:
        frames = [
            sampled.stdout[offset : offset + sample_size]
            for offset in range(0, len(sampled.stdout), sample_size)
        ]
        samples = [
            {
                "mean_luma": round(statistics.mean(frame), 3),
                "spatial_stdev": round(statistics.pstdev(frame), 3),
            }
            for frame in frames
        ]
        if max(sample["spatial_stdev"] for sample in samples) < 1.0:
            errors.append({"check": "blank", "detail": "sampled frames lack detail"})
        diffs = [
            statistics.mean(abs(left - right) for left, right in zip(frames[0], frame))
            for frame in frames[1:]
        ]
        if max(diffs, default=0.0) < 0.5:
            errors.append({"check": "frozen", "detail": "sampled frames do not change"})
    return {
        "valid": not errors,
        "errors": errors,
        "stream": stream,
        "sampled_frames": samples,
    }


def _multipart(fields: Mapping[str, str]) -> tuple[bytes, str]:
    boundary = f"npa-cosmos3-{os.urandom(12).hex()}"
    body = b"".join(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
            f"{value}\r\n"
        ).encode()
        for key, value in fields.items()
    ) + f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _request_fields(prompt: str, negative: str, seed: int) -> dict[str, str]:
    return {
        "prompt": prompt,
        "negative_prompt": negative,
        "size": "1280x720",
        "num_frames": "189",
        "fps": "24",
        "num_inference_steps": "35",
        "guidance_scale": "6.0",
        "flow_shift": "10.0",
        "max_sequence_length": "4096",
        "seed": str(seed),
        "extra_params": json.dumps(
            {
                "use_resolution_template": False,
                "use_duration_template": False,
                "guardrails": False,
            },
            separators=(",", ":"),
        ),
    }


def one_attempt(
    *,
    url: str,
    prompt: str,
    negative_prompt: str,
    prompt_hashes: Mapping[str, str],
    seed: int,
    attempt_id: str,
    replica: int,
    clip_path: Path,
    kind: str,
) -> dict[str, Any]:
    body, content_type = _multipart(_request_fields(prompt, negative_prompt, seed))
    started_at = _utc_now()
    started = time.monotonic()
    record: dict[str, Any] = {
        "schema_version": ATTEMPT_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "kind": kind,
        "replica": replica,
        "seed": seed,
        **prompt_hashes,
        "started_at": started_at,
        "finished_at": "",
        "client_wall_seconds": 0.0,
        "http_status": None,
        "output_bytes": 0,
        "output_sha256": "",
        "technical_valid": False,
        "validation": None,
        "failure_reason": None,
    }
    try:
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Accept": "video/mp4", "Content-Type": content_type},
        )
        with urllib.request.urlopen(request, timeout=SYNC_TIMEOUT_SECONDS) as response:
            payload = response.read()
            record["http_status"] = response.status
        record["output_bytes"] = len(payload)
        record["output_sha256"] = _sha256(payload)
        if response.status != 200:
            record["failure_reason"] = f"http_{response.status}"
        elif not payload:
            record["failure_reason"] = "empty_response"
        else:
            clip_path.parent.mkdir(parents=True, exist_ok=True)
            clip_path.write_bytes(payload)
            validation = validate_video(clip_path)
            record["validation"] = validation
            record["technical_valid"] = bool(validation["valid"])
            if not validation["valid"]:
                checks = ",".join(item["check"] for item in validation["errors"])
                record["failure_reason"] = f"video_invalid:{checks}"
    except urllib.error.HTTPError as exc:
        record["http_status"] = exc.code
        record["failure_reason"] = f"http_{exc.code}"
    except Exception as exc:  # noqa: BLE001 - every attempt remains in the denominator
        record["failure_reason"] = f"{type(exc).__name__}:{exc}"
    record["client_wall_seconds"] = round(time.monotonic() - started, 6)
    record["finished_at"] = _utc_now()
    return record


def _strict_valid(record: Mapping[str, Any]) -> bool:
    return bool(
        record.get("http_status") == 200
        and int(record.get("output_bytes") or 0) > 0
        and record.get("technical_valid") is True
        and record.get("failure_reason") is None
        and isinstance(record.get("validation"), Mapping)
        and record["validation"].get("valid") is True
    )


def derive_cell(records: Sequence[Mapping[str, Any]], window_seconds: float) -> dict[str, Any]:
    if window_seconds <= 0:
        raise Cosmos3SuperBenchmarkError("measurement window must be positive")
    valid = [record for record in records if _strict_valid(record)]
    latencies = [float(record["client_wall_seconds"]) for record in valid]
    video_seconds = len(valid) * VIDEO_SECONDS
    return {
        "attempts": len(records),
        "valid_attempts": len(valid),
        "failed_attempts": len(records) - len(valid),
        "technical_validity_yield": round(len(valid) / len(records), 6)
        if records
        else None,
        "mean_request_latency_seconds": round(statistics.mean(latencies), 3)
        if latencies
        else None,
        "median_request_latency_seconds": round(statistics.median(latencies), 3)
        if latencies
        else None,
        "window_seconds": round(window_seconds, 6),
        "credited_valid_video_seconds": round(video_seconds, 3),
        "valid_video_seconds_per_node_hour": round(video_seconds * 3600 / window_seconds, 1),
        "failed_attempt_video_seconds_credit": 0,
    }


AttemptFn = Callable[..., dict[str, Any]]


def dispatch_cell(
    *,
    topology: Topology,
    urls: Sequence[str],
    attempts: int,
    prompt: str,
    negative_prompt: str,
    prompt_hashes: Mapping[str, str],
    clips_dir: Path,
    kind: str,
    attempt_fn: AttemptFn = one_attempt,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if len(urls) != topology.services:
        raise Cosmos3SuperBenchmarkError("URL count does not match service topology")
    if attempts % topology.services:
        raise Cosmos3SuperBenchmarkError("attempts must divide evenly across services")
    per_service = attempts // topology.services
    barrier = threading.Barrier(topology.services)
    boundary_lock = threading.Lock()
    first_dispatch: float | None = None
    final_completion: float | None = None

    def worker(replica: int) -> list[dict[str, Any]]:
        nonlocal first_dispatch, final_completion
        barrier.wait()
        rows = []
        for index in range(per_service):
            attempt_id = f"{kind}-{topology.name}-r{replica}-a{index:03d}"
            dispatched = time.monotonic()
            with boundary_lock:
                if first_dispatch is None or dispatched < first_dispatch:
                    first_dispatch = dispatched
            row = attempt_fn(
                url=urls[replica],
                prompt=prompt,
                negative_prompt=negative_prompt,
                prompt_hashes=prompt_hashes,
                seed=SEEDS[index % len(SEEDS)],
                attempt_id=attempt_id,
                replica=replica,
                clip_path=clips_dir / f"{attempt_id}.mp4",
                kind=kind,
            )
            completed = time.monotonic()
            with boundary_lock:
                if final_completion is None or completed > final_completion:
                    final_completion = completed
            rows.append(row)
        return rows

    with concurrent.futures.ThreadPoolExecutor(max_workers=topology.services) as pool:
        groups = list(pool.map(worker, range(topology.services)))
    if first_dispatch is None or final_completion is None:
        raise Cosmos3SuperBenchmarkError("measurement produced no request boundaries")
    rows = [row for group in groups for row in group]
    rows.sort(key=lambda row: str(row["attempt_id"]))
    window = {
        "started_at": min(str(row["started_at"]) for row in rows),
        "finished_at": max(str(row["finished_at"]) for row in rows),
        "seconds": round(final_completion - first_dispatch, 6),
        "boundary": (
            "shared first-dispatch-to-final-completion client window; includes routing, "
            "generation, MP4 encoding, uneven completion, failures, and tail idle time; "
            "excludes startup, model load, and warmup"
        ),
    }
    return rows, window


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _publish(local_dir: Path, output_path: str, storage_client: Any = None) -> str:
    if output_path.startswith("s3://"):
        return str(
            (storage_client or StorageClient.from_environment()).upload_directory(
                str(local_dir), output_path.rstrip("/") + "/"
            )
        )
    target = Path(output_path)
    if target.exists():
        raise Cosmos3SuperBenchmarkError(f"local output already exists: {target}")
    shutil.copytree(local_dir, target)
    return str(target)


def run_benchmark(
    *,
    output_path: str,
    topologies: str | Sequence[str] = TOPOLOGY_ORDER,
    attempts: int = 24,
    base_port: int = 8100,
    run_id: str = "",
    dry_run: bool = False,
    storage_client: Any = None,
) -> dict[str, Any]:
    """Run and publish the fixed primary B200 topology sweep."""

    plan = benchmark_plan(
        output_path=output_path, topologies=topologies, attempts=attempts
    )
    plan["run_id"] = run_id
    if dry_run:
        return plan
    if os.environ.get("NPA_COSMOS3_ACCEPT_NVIDIA_SOFTWARE_LICENSE") != "YES":
        raise Cosmos3SuperBenchmarkError(
            "set NPA_COSMOS3_ACCEPT_NVIDIA_SOFTWARE_LICENSE=YES for this run after "
            "reviewing the vLLM-Omni container's NVIDIA runtime terms"
        )
    gpu = _require_b200()
    prompt, negative, prompt_hashes = _load_anchor_prompts()
    with tempfile.TemporaryDirectory(prefix="npa-cosmos3-super-b200-") as tmp:
        root = Path(tmp)
        cells: dict[str, Any] = {}
        for name in parse_topologies(topologies):
            topology = TOPOLOGIES[name]
            cell_dir = root / "cells" / name
            log_dir = root / "private-service-logs" / name
            with running_services(topology, base_port=base_port, work_dir=log_dir) as urls:
                warmups, _ = dispatch_cell(
                    topology=topology,
                    urls=urls,
                    attempts=topology.services,
                    prompt=prompt,
                    negative_prompt=negative,
                    prompt_hashes=prompt_hashes,
                    clips_dir=cell_dir / "warmup-clips",
                    kind="warmup",
                )
                if not all(_strict_valid(row) for row in warmups):
                    _write_json(cell_dir / "warmups.json", warmups)
                    raise Cosmos3SuperBenchmarkError(
                        f"{name} warmup validation failed; measurement window was not opened"
                    )
                shutil.rmtree(cell_dir / "warmup-clips", ignore_errors=True)
                records, window = dispatch_cell(
                    topology=topology,
                    urls=urls,
                    attempts=attempts,
                    prompt=prompt,
                    negative_prompt=negative,
                    prompt_hashes=prompt_hashes,
                    clips_dir=cell_dir / "clips",
                    kind="production",
                )
            derived = derive_cell(records, float(window["seconds"]))
            _write_json(cell_dir / "attempts.json", records)
            _write_json(cell_dir / "window.json", window)
            _write_json(cell_dir / "derived.json", derived)
            cells[name] = {
                "topology": {
                    "services": topology.services,
                    "gpus_per_service": topology.gpus_per_service,
                    "server_parallelism": list(topology.server_args),
                    "request_concurrency_per_service": 1,
                    "warmups_per_service": 1,
                },
                "warmups": warmups,
                "attempts": records,
                "window": window,
                "derived": derived,
            }
        shutil.rmtree(root / "private-service-logs", ignore_errors=True)
        artifact_uri = output_path.rstrip("/") + "/" if output_path.startswith("s3://") else output_path
        report = {
            **plan,
            "status": "succeeded"
            if all(cell["derived"]["failed_attempts"] == 0 for cell in cells.values())
            else "completed_with_invalid_attempts",
            "run_id": run_id,
            "gpu": gpu,
            "prompt_hashes": prompt_hashes,
            "cells": cells,
            "completed_at": _utc_now(),
            "measurement_claim": "technical validity only; semantic quality was not measured",
            "artifact_uri": artifact_uri,
        }
        _write_json(root / "benchmark.json", report)
        published = _publish(root, output_path, storage_client=storage_client)
        report["artifact_uri"] = published
        if report["status"] != "succeeded":
            raise Cosmos3SuperBenchmarkError(
                f"benchmark completed with invalid attempts; evidence retained at {published}"
            )
        return report


__all__ = [
    "ATTEMPT_SCHEMA_VERSION",
    "Cosmos3SuperBenchmarkError",
    "IMAGE",
    "MODEL_ID",
    "MODEL_REVISION",
    "SCHEMA_VERSION",
    "TOPOLOGIES",
    "TOPOLOGY_ORDER",
    "WORKLOAD",
    "benchmark_plan",
    "derive_cell",
    "dispatch_cell",
    "parse_topologies",
    "run_benchmark",
    "service_command",
    "validate_video",
]
