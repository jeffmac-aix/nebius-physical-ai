"""Reproducible bounded-tool benchmark for OpenAI-compatible NPA agents."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import httpx
import typer

from npa.agent_backend.actions import ToolSpec, run_action_loop
from npa.lifecycle_intent import json_stdout_contract

REPORT_SCHEMA = "npa.agent.benchmark.v1"
STATE_SCHEMA = "npa.agent.benchmark.state.v1"
DEFAULT_MODEL = "deepseek-v4-flash-0731"
DEFAULT_SPEC = "npa/workflows/workbench/npa-workflows/paidf-cosmos3.yaml"
_SECRET_RE = re.compile(
    r"(?i)(authorization|api[_-]?key|secret|password|token)([\"'=:\s]+)([^\s,}\]]+)"
)
_OPAQUE_ID_RE = re.compile(
    r"(?<![a-z0-9])(?:tenant|project|cluster|bucket|registry)-[a-z0-9][a-z0-9-]{6,}"
)


class OutputFormat(str, Enum):
    text = "text"
    json = "json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Any, *, length: int = 16) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _redact_text(text: str, replacements: Mapping[str, str] | None = None) -> str:
    value = str(text or "")
    for raw, label in sorted((replacements or {}).items(), key=lambda item: -len(item[0])):
        if raw:
            value = value.replace(raw, label)
    value = _SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", value)
    return _OPAQUE_ID_RE.sub("<live-resource>", value)


def _sanitize(value: Any, replacements: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return _redact_text(value, replacements)
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if re.search(r"(?i)(api_key|secret|password|authorization|credential_value)", name):
                sanitized[name] = "<redacted>"
            else:
                sanitized[name] = _sanitize(item, replacements)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, replacements) for item in value]
    return value


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
        raise ValueError(f"unsupported benchmark state in {path}")
    return data


@dataclass
class StreamingPlanner:
    """TLS-verified streaming chat-completions client with per-call metrics."""

    endpoint: str
    model: str
    api_key: str
    timeout_s: float = 180.0
    records: list[dict[str, Any]] = field(default_factory=list)
    on_record: Callable[[], None] | None = None
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if not self.endpoint.startswith("https://"):
            raise ValueError("benchmark endpoint must use https://")
        self.endpoint = self.endpoint.rstrip("/")
        if self.endpoint.endswith("/chat/completions"):
            self.url = self.endpoint
        else:
            self.url = f"{self.endpoint}/chat/completions"
        self.timeout_s = max(180.0, float(self.timeout_s))

    def __call__(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tier: str = "standard",
        phase: str = "agent",
    ) -> dict[str, Any]:
        del tier
        started_wall = _utc_now()
        started = time.monotonic()
        retries: list[dict[str, Any]] = []
        response_data: dict[str, Any] | None = None
        content = ""
        reasoning = ""
        usage: dict[str, Any] = {}
        first_token_at: float | None = None
        finish_reason = ""
        for attempt in range(3):
            content = ""
            reasoning = ""
            usage = {}
            first_token_at = None
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": list(messages),
                "temperature": 0.1,
                "stream": True,
            }
            if attempt == 0:
                payload["stream_options"] = {"include_usage": True}
            try:
                timeout = httpx.Timeout(self.timeout_s, connect=60.0)
                with httpx.Client(
                    timeout=timeout,
                    verify=True,
                    transport=self.transport,
                ) as client:
                    with client.stream(
                        "POST",
                        self.url,
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    ) as response:
                        response.raise_for_status()
                        response_id = ""
                        for line in response.iter_lines():
                            if not line.startswith("data:"):
                                continue
                            raw = line[5:].strip()
                            if not raw or raw == "[DONE]":
                                continue
                            chunk = json.loads(raw)
                            response_id = str(chunk.get("id") or response_id)
                            if isinstance(chunk.get("usage"), dict):
                                usage = dict(chunk["usage"])
                            choices = chunk.get("choices")
                            if not isinstance(choices, list) or not choices:
                                continue
                            choice = choices[0] if isinstance(choices[0], dict) else {}
                            finish_reason = str(choice.get("finish_reason") or finish_reason)
                            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                            piece = delta.get("content")
                            thought = delta.get("reasoning_content") or delta.get("reasoning")
                            if piece:
                                content += str(piece)
                            if thought:
                                reasoning += str(thought)
                            if first_token_at is None and (piece or thought):
                                first_token_at = time.monotonic()
                        response_data = {
                            "id": response_id,
                            "model": self.model,
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": content,
                                        **({"reasoning_content": reasoning} if reasoning else {}),
                                    },
                                    "finish_reason": finish_reason,
                                }
                            ],
                            "usage": usage,
                        }
                break
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", 0)
                retries.append(
                    {
                        "attempt": attempt + 1,
                        "status_code": int(status or 0),
                        "error_type": type(exc).__name__,
                    }
                )
                transient = status in {0, 408, 409, 425, 429} or status >= 500
                # A provider that rejects stream_options is retried without it.
                if attempt == 0 and status in {400, 404, 422}:
                    transient = True
                if not transient or attempt == 2:
                    ended = time.monotonic()
                    self._record(
                        phase=phase,
                        started_at=started_wall,
                        latency_s=ended - started,
                        ttft_s=None,
                        usage={},
                        output_chars=0,
                        finish_reason="error",
                        retries=retries,
                        error=type(exc).__name__,
                        message_count=len(messages),
                        input_chars=sum(len(str(message.get("content") or "")) for message in messages),
                    )
                    raise RuntimeError(
                        f"provider request failed after {attempt + 1} attempt(s); status={status or 'transport'}"
                    ) from exc
                time.sleep(0.6 * (2**attempt))
        assert response_data is not None
        ended = time.monotonic()
        ttft = (first_token_at - started) if first_token_at is not None else None
        self._record(
            phase=phase,
            started_at=started_wall,
            latency_s=ended - started,
            ttft_s=ttft,
            usage=usage,
            output_chars=len(content),
            finish_reason=finish_reason,
            retries=retries,
            error="",
            message_count=len(messages),
            input_chars=sum(len(str(message.get("content") or "")) for message in messages),
        )
        return response_data

    def _record(self, **values: Any) -> None:
        completion = values.get("usage", {}).get("completion_tokens")
        ttft = values.get("ttft_s")
        latency = float(values.get("latency_s") or 0.0)
        throughput = None
        if isinstance(completion, (int, float)) and ttft is not None and latency > float(ttft):
            throughput = float(completion) / (latency - float(ttft))
        record = {
            "call_index": 0,
            **values,
            "latency_s": round(latency, 6),
            "ttft_s": round(float(ttft), 6) if ttft is not None else None,
            "output_tokens_per_s": round(throughput, 6) if throughput is not None else None,
            "response_digest": _digest(
                {"output_chars": values.get("output_chars"), "finish": values.get("finish_reason")}
            ),
        }
        with self._lock:
            record["call_index"] = len(self.records) + 1
            self.records.append(record)
        if self.on_record:
            self.on_record()


def _command_json(argv: Sequence[str], *, cwd: Path) -> dict[str, Any]:
    started = time.monotonic()
    result = subprocess.run(
        list(argv),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    elapsed = time.monotonic() - started
    payload: Any = None
    raw = result.stdout.strip()
    if raw:
        decoder = json.JSONDecoder()
        for index, char in enumerate(raw):
            if char not in "[{":
                continue
            try:
                payload, _ = decoder.raw_decode(raw[index:])
                break
            except json.JSONDecodeError:
                continue
    if not isinstance(payload, dict):
        payload = {"stdout": raw[-4000:] if raw else ""}
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "elapsed_s": round(elapsed, 6),
        "result": payload,
        **({"stderr": result.stderr.strip()[-2000:]} if result.stderr.strip() else {}),
    }


class BenchmarkToolbox:
    """Fixed-argv NPA executors; the model never receives a shell primitive."""

    ORDER = (
        "inspect_environment",
        "inspect_capabilities",
        "inspect_repository_context",
        "health_preflight",
        "health_access",
        "workflow_validate",
        "workflow_plan",
        "infra_plan",
        "infra_provision",
        "skypilot_bootstrap",
        "skypilot_verify",
        "workflow_preflight_images",
        "workflow_submit",
        "workflow_status",
        "workflow_artifacts",
    )
    MUTATING = frozenset({"infra_provision", "skypilot_bootstrap", "workflow_submit"})

    def __init__(
        self,
        *,
        repo: Path,
        state: dict[str, Any],
        save: Callable[[], None],
        project: str,
        cluster: str,
        bucket: str,
        accelerator: str,
        spec: Path,
    ) -> None:
        self.repo = repo
        self.state = state
        self.save = save
        self.project = project
        self.cluster = cluster
        self.bucket = bucket
        self.accelerator = accelerator
        self.spec = spec
        self.npa = str(repo / "npa" / ".venv" / "bin" / "npa")
        self.run_id = str(state["run_id"])
        self.operation_digest = str(state["operation_digest"])
        self.replacements = {
            project: "<project-alias>",
            cluster: "<cluster-context>",
            bucket: "<bucket>",
            self.run_id: "<run-id>",
        }

    @classmethod
    def allowlist(cls) -> dict[str, ToolSpec]:
        summaries = {
            "inspect_environment": "Inspect NPA version and benchmark target without credentials.",
            "inspect_capabilities": "Inspect the bounded tool catalog and selected workflow metadata.",
            "inspect_repository_context": "Read representative bounded public repo/workflow context.",
            "health_preflight": "Run live HF/NGC/S3/hosted-inference credential preflight.",
            "health_access": "Probe actual Cosmos3 and PAIDF gated-model access.",
            "workflow_validate": "Validate the selected paidf-cosmos3 npa.workflow offline.",
            "workflow_plan": "Plan the accepted path with one synthetic variant.",
            "infra_plan": "Run additive provision-if-absent dry-run for the selected GPU.",
            "infra_provision": "Apply additive NPA provisioning/validation for the fixed target.",
            "skypilot_bootstrap": "Install/repair the pinned SkyPilot runtime locally.",
            "skypilot_verify": "Verify the exact Kubernetes context through NPA.",
            "workflow_preflight_images": "Prove every selected workflow image is pullable.",
            "workflow_submit": "Submit the fixed seed-fixture runtime workflow and wait without a deadline.",
            "workflow_status": "Read durable/live workflow status for the fixed run id.",
            "workflow_artifacts": "List durable artifacts for the fixed run id.",
        }
        specs: dict[str, ToolSpec] = {}
        for name in cls.ORDER:
            params: tuple[str, ...] = ()
            if name in cls.MUTATING:
                params = ("operation_digest",)
            elif name in {"workflow_status", "workflow_artifacts"}:
                params = ("run_id",)
            specs[name] = ToolSpec(
                name,
                read_only=name not in cls.MUTATING,
                requires_confirmation=name in cls.MUTATING,
                summary=summaries[name],
                params=params,
            )
        return specs

    def executors(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {name: (lambda args, selected=name: self.execute(selected, args)) for name in self.ORDER}

    def execute(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if name in self.MUTATING and str(args.get("operation_digest") or "") != self.operation_digest:
            return {"ok": False, "error": "operation_digest must match the fixed benchmark operation"}
        if name in {"workflow_status", "workflow_artifacts"} and str(args.get("run_id") or "") != self.run_id:
            return {"ok": False, "error": "run_id must match the fixed benchmark run"}
        prerequisites = {
            "infra_provision": {"health_access", "infra_plan"},
            "skypilot_verify": {"infra_provision", "skypilot_bootstrap"},
            "workflow_preflight_images": {"workflow_plan", "skypilot_verify"},
            "workflow_submit": {"health_access", "workflow_plan", "workflow_preflight_images"},
            "workflow_status": {"workflow_submit"},
            "workflow_artifacts": {"workflow_submit"},
        }
        completed = set(self.state.get("completed_tools") or [])
        missing = sorted(prerequisites.get(name, set()) - completed)
        if missing:
            return {"ok": False, "error": "missing prerequisite observations: " + ", ".join(missing)}
        started = time.monotonic()
        try:
            result = self._execute(name)
        except Exception as exc:  # noqa: BLE001 - turn fixed tool failures into observations
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        elapsed = time.monotonic() - started
        sanitized = _sanitize(result, self.replacements)
        record = {
            "tool": name,
            "started_at": _utc_now(),
            "elapsed_s": round(elapsed, 6),
            "ok": bool(result.get("ok")),
            "observation": sanitized,
            "observation_digest": _digest(sanitized),
        }
        self.state.setdefault("tool_calls", []).append(record)
        if record["ok"] and name not in completed:
            self.state.setdefault("completed_tools", []).append(name)
        self.save()
        return sanitized

    def _execute(self, name: str) -> dict[str, Any]:
        if name == "inspect_environment":
            version = subprocess.run(
                [self.npa, "--version"], cwd=self.repo, text=True, capture_output=True, check=False
            )
            return {
                "ok": version.returncode == 0,
                "npa_version": version.stdout.strip(),
                "workflow": self.spec.relative_to(self.repo).as_posix(),
                "workflow_sha256": hashlib.sha256(self.spec.read_bytes()).hexdigest(),
                "accelerator": self.accelerator,
                "seed_fixture": True,
            }
        if name == "inspect_capabilities":
            return {
                "ok": True,
                "allowlisted_tools": list(self.ORDER),
                "arbitrary_shell": False,
                "workflow_api_version": "npa.workflow/v0.0.1",
                "selected_workflow": "paidf-cosmos3",
            }
        if name == "inspect_repository_context":
            context, manifest = representative_context(self.repo, max_chars=12_000)
            return {"ok": True, "context_chars": len(context), "files": manifest, "excerpt": context}
        if name == "health_preflight":
            return _command_json(
                [self.npa, "workbench", "health", "preflight", "--checks", "hf,ngc,s3,token_factory", "--json"],
                cwd=self.repo,
            )
        if name == "health_access":
            return _command_json(
                [self.npa, "workbench", "health", "access", "--capability", "cosmos3,paidf", "--json"],
                cwd=self.repo,
            )
        if name == "workflow_validate":
            return _command_json(
                [self.npa, "workbench", "workflow", "validate-spec", str(self.spec), "--json"], cwd=self.repo
            )
        if name == "workflow_plan":
            return _command_json(
                [
                    self.npa,
                    "workbench",
                    "workflow",
                    "plan-spec",
                    str(self.spec),
                    "--run-id",
                    self.run_id,
                    "--assume-decision",
                    "promote_checkpoint",
                    "--var",
                    f"bucket={self.bucket}",
                    "--var",
                    "variant_count=1",
                    "--var",
                    "variant_parallelism=1",
                    "--json",
                ],
                cwd=self.repo,
            )
        if name in {"infra_plan", "infra_provision"}:
            argv = [
                self.npa,
                "provision-if-absent",
                "--project",
                self.project,
                "--cluster-name",
                self.cluster,
                "--accelerator",
                self.accelerator,
                "--output-format",
                "json",
            ]
            if name == "infra_plan":
                argv.append("--dry-run")
            return _command_json(argv, cwd=self.repo)
        if name == "skypilot_bootstrap":
            result = _command_json([self.npa, "skypilot", "bootstrap"], cwd=self.repo)
            # bootstrap is a human-text command; retain only a bounded digest in
            # the model observation while its exit code remains authoritative.
            return {
                "ok": bool(result.get("ok")),
                "exit_code": result.get("exit_code"),
                "elapsed_s": result.get("elapsed_s"),
                "output_digest": _digest(result),
            }
        if name == "skypilot_verify":
            return _command_json(
                [self.npa, "skypilot", "verify", "--cluster", self.cluster, "--output-format", "json"], cwd=self.repo
            )
        if name == "workflow_preflight_images":
            return _command_json(
                [
                    self.npa,
                    "workbench",
                    "workflow",
                    "preflight-images",
                    str(self.spec),
                    "--project",
                    self.project,
                    "--var",
                    f"bucket={self.bucket}",
                    "--var",
                    "variant_count=1",
                    "--var",
                    "variant_parallelism=1",
                    "--assume-decision",
                    "promote_checkpoint",
                    "--infra",
                    f"k8s/{self.cluster}",
                    "--json",
                ],
                cwd=self.repo,
            )
        if name == "workflow_submit":
            self.state["submission_intent"] = {"run_id": self.run_id, "recorded_at": _utc_now()}
            self.save()
            return _command_json(
                [
                    self.npa,
                    "workbench",
                    "workflow",
                    "submit",
                    str(self.spec),
                    "--run-id",
                    self.run_id,
                    "--runtime",
                    "--max-wait-seconds",
                    "0",
                    "--no-cancel-on-timeout",
                    "--assume-decision",
                    "promote_checkpoint",
                    "--var",
                    f"bucket={self.bucket}",
                    "--var",
                    "variant_count=1",
                    "--var",
                    "variant_parallelism=1",
                    "--infra",
                    f"k8s/{self.cluster}",
                    "--project",
                    self.project,
                    "--seed-fixture",
                    "--secret-env",
                    "HF_TOKEN",
                    "--secret-env",
                    "NEBIUS_TOKEN_FACTORY_KEY",
                    "--secret-env",
                    "AWS_ACCESS_KEY_ID",
                    "--secret-env",
                    "AWS_SECRET_ACCESS_KEY",
                    "--output-format",
                    "json",
                ],
                cwd=self.repo,
            )
        if name == "workflow_status":
            return _command_json(
                [self.npa, "workbench", "workflow", "status", self.run_id, "--project", self.project, "--json"],
                cwd=self.repo,
            )
        if name == "workflow_artifacts":
            return _command_json(
                [self.npa, "workbench", "workflow", "artifacts", self.run_id, "--project", self.project, "--json"],
                cwd=self.repo,
            )
        raise ValueError(f"unsupported benchmark tool: {name}")


def representative_context(repo: Path, *, max_chars: int = 60_000) -> tuple[str, list[dict[str, Any]]]:
    """Build meaningful high-context input from public repository sources only."""
    relative_paths = (
        "AGENTS.md",
        "skills/atomic/agent-development/SKILL.md",
        "skills/workflows/cosmos3-inference/SKILL.md",
        "skills/workflows/physical-ai-data-factory/SKILL.md",
        "docs/workbench/guides/paidf-cosmos3.md",
        DEFAULT_SPEC,
    )
    chunks: list[str] = []
    manifest: list[dict[str, Any]] = []
    remaining = max(1, int(max_chars))
    for relative in relative_paths:
        path = (repo / relative).resolve()
        if not path.is_relative_to(repo.resolve()) or not path.is_file():
            continue
        text = path.read_text(errors="replace")
        selected = text[:remaining]
        chunks.append(f"\n--- {relative} ---\n{selected}")
        manifest.append(
            {
                "path": relative,
                "source_chars": len(text),
                "included_chars": len(selected),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
        remaining -= len(selected)
        if remaining <= 0:
            break
    return "".join(chunks), manifest


def _context_probe(planner: StreamingPlanner, repo: Path, context_chars: int) -> dict[str, Any]:
    context, manifest = representative_context(repo, max_chars=context_chars)
    response = planner(
        [
            {
                "role": "system",
                "content": (
                    "You are evaluating a Physical AI repository. Return one JSON object with keys "
                    "workflow, safety_gates, gpu_compatibility, and first_live_action. Use only the supplied context."
                ),
            },
            {"role": "user", "content": context},
        ],
        phase="high_context",
    )
    content = str(response["choices"][0]["message"].get("content") or "")
    return {
        "context_chars": len(context),
        "files": manifest,
        "response_chars": len(content),
        "response_digest": _digest(content),
        "advertised_vs_tested": "tested",
    }


def _concurrency_probe(planner: StreamingPlanner, concurrency: int) -> dict[str, Any]:
    count = max(1, int(concurrency))
    started = time.monotonic()

    def call(index: int) -> dict[str, Any]:
        response = planner(
            [
                {"role": "system", "content": "Reply with one JSON object and no prose."},
                {
                    "role": "user",
                    "content": (
                        "Classify this NPA operation as read_only or state_changing: "
                        + ("workflow validate-spec" if index % 2 == 0 else "workflow submit")
                    ),
                },
            ],
            phase="concurrency_probe",
        )
        return {"index": index, "response_digest": _digest(response)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(call, range(count)))
    return {
        "requested_concurrency": count,
        "completed_calls": len(results),
        "wall_s": round(time.monotonic() - started, 6),
        "errors": [],
        "response_digests": [item["response_digest"] for item in results],
    }


def _baseline(toolbox: BenchmarkToolbox) -> dict[str, Any]:
    names = (
        "inspect_environment",
        "inspect_capabilities",
        "inspect_repository_context",
        "health_preflight",
        "health_access",
        "workflow_validate",
        "workflow_plan",
        "infra_plan",
    )
    started = time.monotonic()
    steps: list[dict[str, Any]] = []
    for name in names:
        before = time.monotonic()
        result = toolbox._execute(name)  # deterministic baseline intentionally bypasses agent state
        sanitized = _sanitize(result, toolbox.replacements)
        steps.append(
            {
                "tool": name,
                "ok": bool(result.get("ok")),
                "elapsed_s": round(time.monotonic() - before, 6),
                "observation_digest": _digest(sanitized),
            }
        )
    return {
        "label": "deterministic_read_only_baseline",
        "agentic": False,
        "wall_s": round(time.monotonic() - started, 6),
        "steps": steps,
        "successful_steps": sum(1 for step in steps if step["ok"]),
    }


def _execution_evidence(state: Mapping[str, Any]) -> dict[str, Any]:
    """Extract numeric runtime evidence without assuming one status response shape."""
    stage_timings: list[dict[str, Any]] = []
    resource_measurements: list[dict[str, Any]] = []
    timing_names = {
        "duration_s",
        "elapsed_s",
        "runtime_s",
        "runtime_seconds",
        "wall_s",
    }
    resource_names = {
        "gpu_seconds",
        "gpu_runtime_s",
        "resource_seconds",
        "cpu_seconds",
    }

    def visit(value: Any, *, path: str, stage: str = "") -> None:
        if isinstance(value, Mapping):
            next_stage = str(
                value.get("stage")
                or value.get("stage_id")
                or value.get("state")
                or value.get("name")
                or stage
            )
            for key, item in value.items():
                item_path = f"{path}.{key}" if path else str(key)
                if key in timing_names and isinstance(item, (int, float)):
                    stage_timings.append(
                        {"stage": next_stage or path or "workflow", "metric": key, "seconds": item}
                    )
                if key in resource_names and isinstance(item, (int, float)):
                    resource_measurements.append(
                        {"stage": next_stage or path or "workflow", "metric": key, "value": item}
                    )
                visit(item, path=item_path, stage=next_stage)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, path=f"{path}[{index}]", stage=stage)

    relevant = [
        call
        for call in state.get("tool_calls") or []
        if call.get("tool") in {"workflow_submit", "workflow_status", "workflow_artifacts"}
    ]
    for call in relevant:
        visit(call.get("observation"), path=str(call.get("tool") or "workflow"))
    return {
        "stage_timings": stage_timings,
        "resource_measurements": resource_measurements,
        "source_tool_calls": len(relevant),
        "availability": "measured" if stage_timings or resource_measurements else "not_reported",
    }


def _summary(state: Mapping[str, Any], planner: StreamingPlanner, started: float) -> dict[str, Any]:
    usage_keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    usage: dict[str, int | None] = {}
    for key in usage_keys:
        values = [record.get("usage", {}).get(key) for record in planner.records]
        numeric = [int(value) for value in values if isinstance(value, (int, float))]
        usage[key] = sum(numeric) if len(numeric) == len(values) and values else None
    retries = sum(len(record.get("retries") or []) for record in planner.records)
    return {
        "wall_s": round(time.monotonic() - started, 6),
        "agent_rounds": len(state.get("agent_rounds") or []),
        "model_calls": len(planner.records),
        "tool_calls": len(state.get("tool_calls") or []),
        "completed_tools": list(state.get("completed_tools") or []),
        "usage": usage,
        "retries": retries,
        "errors": [record["error"] for record in planner.records if record.get("error")],
        "execution_evidence": _execution_evidence(state),
        "cost": {
            "monetary": None,
            "currency": None,
            "status": "unavailable",
            "reason": "No authoritative per-request or live-resource billing record was returned.",
            "measured_token_usage": usage,
        },
    }


@json_stdout_contract
def benchmark_cmd(
    project: str = typer.Option(..., "--project", help="Configured task project alias."),
    cluster: str = typer.Option(..., "--cluster", help="Exact configured Kubernetes context."),
    bucket: str = typer.Option(..., "--bucket", help="Configured task bucket; never written to public output."),
    accelerator: str = typer.Option(..., "--accelerator", help="Compatible requestable accelerator name/count."),
    endpoint: str = typer.Option(..., "--endpoint", help="TLS OpenAI-compatible base URL."),
    model: str = typer.Option(DEFAULT_MODEL, "--model"),
    api_key_file: Path | None = typer.Option(None, "--api-key-file", exists=True, dir_okay=False),
    api_key_env: str = typer.Option("NPA_AGENT_BENCHMARK_API_KEY", "--api-key-env"),
    spec: Path = typer.Option(Path(DEFAULT_SPEC), "--spec", exists=True, dir_okay=False),
    state_path: Path = typer.Option(..., "--state-path", help="Owner-only resumable state outside Git."),
    report_path: Path = typer.Option(..., "--report-path", help="Sanitized machine-readable report."),
    confirm_action: list[str] | None = typer.Option(
        None,
        "--confirm-action",
        help="Task-scoped authorization for a named mutating tool; repeat for each action class.",
    ),
    context_chars: int = typer.Option(60_000, "--context-chars", min=1),
    concurrency: int = typer.Option(2, "--concurrency", min=1),
    run_baseline: bool = typer.Option(True, "--baseline/--no-baseline"),
    output_format: OutputFormat = typer.Option(OutputFormat.text, "--output-format"),
) -> None:
    """Benchmark a real model-guided NPA setup-to-workflow loop with bounded tools."""
    repo = Path.cwd().resolve()
    resolved_spec = (repo / spec).resolve() if not spec.is_absolute() else spec.resolve()
    if not resolved_spec.is_relative_to(repo) or resolved_spec.name != "paidf-cosmos3.yaml":
        raise typer.BadParameter("--spec must resolve to the repository paidf-cosmos3.yaml")
    key = ""
    if api_key_file is not None:
        key = api_key_file.read_text().strip()
    if not key:
        key = str(os.environ.get(api_key_env, "")).strip()
    if not key:
        raise typer.BadParameter(f"provider key is missing ({api_key_env} or --api-key-file)")
    authorized = set(confirm_action or [])
    unknown = authorized - BenchmarkToolbox.MUTATING
    if unknown:
        raise typer.BadParameter("unknown --confirm-action value(s): " + ", ".join(sorted(unknown)))

    existing = _load_state(state_path)
    operation = {
        "project": project,
        "cluster": cluster,
        "bucket": bucket,
        "accelerator": accelerator,
        "spec_sha256": hashlib.sha256(resolved_spec.read_bytes()).hexdigest(),
        "seed_fixture": True,
        "variant_count": 1,
        "secret_env_names": [
            "HF_TOKEN",
            "NEBIUS_TOKEN_FACTORY_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
        ],
    }
    operation_digest = _digest(operation, length=24)
    if existing and existing.get("operation_digest") != operation_digest:
        raise typer.BadParameter("state operation does not match the requested benchmark")
    state = existing or {
        "schema": STATE_SCHEMA,
        "created_at": _utc_now(),
        "run_id": f"deepseek-paidf-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "operation_digest": operation_digest,
        "completed_tools": [],
        "tool_calls": [],
        "model_calls": [],
        "agent_rounds": [],
        "authorizations": [],
    }

    def save() -> None:
        state["updated_at"] = _utc_now()
        _atomic_write(state_path, state)

    save()
    planner = StreamingPlanner(endpoint=endpoint, model=model, api_key=key)
    planner.records = list(state.get("model_calls") or [])

    def save_model_records() -> None:
        state["model_calls"] = list(planner.records)
        save()

    planner.on_record = save_model_records
    toolbox = BenchmarkToolbox(
        repo=repo,
        state=state,
        save=save,
        project=project,
        cluster=cluster,
        bucket=bucket,
        accelerator=accelerator,
        spec=resolved_spec,
    )
    started = time.monotonic()
    if "high_context" not in state:
        state["high_context"] = _context_probe(planner, repo, context_chars)
        save()
    if "concurrency" not in state:
        state["concurrency"] = _concurrency_probe(planner, concurrency)
        save()
    if run_baseline and "baseline" not in state:
        state["baseline"] = _baseline(toolbox)
        save()

    allowlist = toolbox.allowlist()
    authorization_events: list[dict[str, Any]] = state.setdefault("authorizations", [])

    def authorize(action: Mapping[str, Any], digest: str) -> bool:
        tool = str(action.get("tool") or "")
        allowed = tool in authorized
        authorization_events.append(
            {
                "tool": tool,
                "action_digest": digest,
                "operation_digest": operation_digest,
                "authorized": allowed,
                "basis": "explicit_task_scope" if allowed else "not_authorized",
                "recorded_at": _utc_now(),
            }
        )
        save()
        return allowed

    while True:
        remaining = [name for name in BenchmarkToolbox.ORDER if name not in set(state["completed_tools"])]
        if not remaining:
            state["status"] = "complete"
            break
        before = set(state["completed_tools"])
        exact_args = {
            "operation_digest": operation_digest,
            "run_id": state["run_id"],
        }
        goal = (
            "Autonomously take the fresh NPA environment through the real paidf-cosmos3 seed-data run. "
            "Call every explicitly named remaining tool with useful arguments, respond to observations, "
            "and never invent success. Remaining tools: "
            + ", ".join(remaining)
            + ". For each mutating tool pass operation_digest="
            + operation_digest
            + "; for workflow_status and workflow_artifacts pass run_id="
            + str(state["run_id"])
            + "."
        )
        live_context = json.dumps(
            {
                "operation": {
                    "digest": operation_digest,
                    "workflow": "paidf-cosmos3",
                    "seed_fixture": True,
                    "accelerator": accelerator,
                },
                "required_exact_args": exact_args,
                "completed_tools": state["completed_tools"],
                "recent_tool_calls": state["tool_calls"][-5:],
            },
            sort_keys=True,
        )
        result = run_action_loop(
            goal,
            tools=toolbox.executors(),
            model_call=lambda messages, tier="standard": planner(
                messages, tier=tier, phase="agent_planner"
            ),
            tier="reasoning",
            max_steps=12,
            allowlist=allowlist,
            live_context=live_context,
            action_authorizer=authorize,
        )
        state["agent_rounds"].append(_sanitize(result, toolbox.replacements))
        save()
        if result.get("needs_confirmation"):
            state["status"] = "needs_confirmation"
            break
        if set(state["completed_tools"]) == before:
            state["status"] = "blocked_no_progress"
            break

    state["finished_at"] = _utc_now()
    summary = _summary(state, planner, started)
    sanitized_state = _sanitize(state, toolbox.replacements)
    report = {
        "schema": REPORT_SCHEMA,
        "status": state.get("status", "unknown"),
        "provider": {
            "kind": "openai_compatible",
            "model": model,
            "tls_verification": True,
            "endpoint_disclosed": False,
        },
        "operation": {
            "digest": operation_digest,
            "workflow": "paidf-cosmos3",
            "seed_fixture": True,
            "variant_count": 1,
            "accelerator": accelerator,
            "run_id_digest": _digest(state["run_id"]),
        },
        "high_context": sanitized_state.get("high_context"),
        "concurrency": sanitized_state.get("concurrency"),
        "baseline": sanitized_state.get("baseline"),
        "model_calls": sanitized_state.get("model_calls", []),
        "tool_calls": sanitized_state.get("tool_calls", []),
        "agent_rounds": sanitized_state.get("agent_rounds", []),
        "authorizations": sanitized_state.get("authorizations", []),
        "summary": summary,
        "limitations": [
            "Monetary cost is unavailable unless an authoritative billing record is returned.",
            "The action loop is sequential; concurrency measurements are separate provider probes.",
        ],
    }
    _atomic_write(report_path, report)
    save()
    if output_format == OutputFormat.json:
        print(json.dumps(report, sort_keys=True))
    else:
        typer.echo(
            f"benchmark status={report['status']} model_calls={summary['model_calls']} "
            f"tool_calls={summary['tool_calls']} report={report_path}"
        )
    if report["status"] != "complete":
        raise typer.Exit(1)


__all__ = [
    "BenchmarkToolbox",
    "REPORT_SCHEMA",
    "STATE_SCHEMA",
    "StreamingPlanner",
    "benchmark_cmd",
    "representative_context",
]
