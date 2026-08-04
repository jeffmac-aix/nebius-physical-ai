"""FastAPI routes for authenticated LeIsaac discovery and WebRTC signaling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from starlette.requests import Request

LOG = logging.getLogger(__name__)

try:  # agent VM: /opt/npa-agent is on sys.path
    from agent_backend.leisaac import (
        LEISAAC_CLIENT_MODULE_PATH,
        LEISAAC_CLIENT_JS_SHA256,
        LEISAAC_SIGNAL_PORT,
        normalize_manifest,
        selected_run_id,
        status_payload,
        validate_health,
    )
except ImportError:  # repository tests
    from npa.agent_backend.leisaac import (
        LEISAAC_CLIENT_MODULE_PATH,
        LEISAAC_CLIENT_JS_SHA256,
        LEISAAC_SIGNAL_PORT,
        normalize_manifest,
        selected_run_id,
        status_payload,
        validate_health,
    )


@dataclass
class LeIsaacDeps:
    """Dependencies supplied by the rendered agent backend."""

    load_state: Callable[[], dict]
    resolve_manifest: Callable[[str], dict | None]
    http_get: Callable[..., Any]
    response: Any
    websocket_connect: Callable[..., Any]


def _resolve(deps: LeIsaacDeps, requested_run_id: str) -> tuple[dict | None, str]:
    run_id = selected_run_id(deps.load_state(), requested_run_id)
    if not run_id:
        return None, "Select a run that exposes a LeIsaac teleoperation session."
    try:
        raw = deps.resolve_manifest(run_id)
    except Exception:  # storage failures are capability absence, not a 500 in the UI
        return None, "LeIsaac capability discovery is unavailable."
    return normalize_manifest(raw, expected_run_id=run_id)


def _health(deps: LeIsaacDeps, manifest: dict) -> tuple[dict | None, str]:
    try:
        response = deps.http_get(
            f"{manifest['service_url']}/status",
            timeout=3.0,
            follow_redirects=False,
        )
        if int(response.status_code) != 200:
            return None, f"LeIsaac service health returned HTTP {response.status_code}"
        payload = response.json()
    except Exception:
        return None, "LeIsaac service is unreachable."
    return validate_health(manifest, payload)


async def _relay_browser_to_upstream(browser: Any, upstream: Any) -> None:
    while True:
        message = await browser.receive()
        kind = message.get("type")
        if kind == "websocket.disconnect":
            return
        if message.get("bytes") is not None:
            await upstream.send(message["bytes"])
        elif message.get("text") is not None:
            await upstream.send(message["text"])


async def _relay_upstream_to_browser(browser: Any, upstream: Any) -> None:
    async for message in upstream:
        if isinstance(message, bytes):
            await browser.send_bytes(message)
        else:
            await browser.send_text(str(message))


def register_leisaac_routes(app: Any, deps: LeIsaacDeps) -> None:
    """Register the LeIsaac capability, client-module, and signaling routes."""

    @app.get("/leisaac/status")
    def leisaac_status(request: Request, run_id: str = "") -> dict:
        if str(request.headers.get("x-forwarded-proto") or "").lower() != "https":
            return status_payload(
                None,
                reason="LeIsaac teleoperation requires the public HTTPS agent endpoint.",
            )
        manifest, reason = _resolve(deps, run_id)
        if not manifest:
            return status_payload(None, reason=reason)
        health, reason = _health(deps, manifest)
        return status_payload(manifest, health, reason=reason)

    @app.get(LEISAAC_CLIENT_MODULE_PATH.removeprefix("/api"))
    def leisaac_client_module(run_id: str = "") -> Any:
        manifest, reason = _resolve(deps, run_id)
        if not manifest:
            return deps.response(
                content=json.dumps({"detail": reason}),
                status_code=404,
                media_type="application/json",
            )
        health, reason = _health(deps, manifest)
        if not health:
            return deps.response(
                content=json.dumps({"detail": reason}),
                status_code=503,
                media_type="application/json",
            )
        try:
            response = deps.http_get(
                f"{manifest['service_url']}/client/index.js",
                timeout=10.0,
                follow_redirects=False,
            )
        except Exception:
            response = None
        if response is None or int(response.status_code) != 200:
            return deps.response(
                content=json.dumps({"detail": "LeIsaac WebRTC client is unavailable"}),
                status_code=502,
                media_type="application/json",
            )
        content = bytes(response.content)
        if (
            len(content) > 2 * 1024 * 1024
            or hashlib.sha256(content).hexdigest() != LEISAAC_CLIENT_JS_SHA256
        ):
            return deps.response(
                content=json.dumps(
                    {"detail": "LeIsaac WebRTC client failed integrity validation"}
                ),
                status_code=502,
                media_type="application/json",
            )
        return deps.response(
            content=content,
            status_code=200,
            media_type="text/javascript",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.websocket("/leisaac/signal")
    async def leisaac_signal(websocket: Any) -> None:
        if str(websocket.headers.get("x-forwarded-proto") or "").lower() != "https":
            await websocket.close(code=1008)
            return
        run_id = str(websocket.query_params.get("run_id") or "")
        manifest, _reason = _resolve(deps, run_id)
        if not manifest:
            await websocket.close(code=1008)
            return
        health, _reason = _health(deps, manifest)
        if not health:
            await websocket.close(code=1013)
            return

        requested = str(websocket.headers.get("sec-websocket-protocol") or "")
        protocols = [item.strip() for item in requested.split(",") if item.strip()]
        protocols = [
            item for item in protocols if len(item) <= 128 and "\n" not in item
        ]
        uri = f"ws://{manifest['signal_host']}:{LEISAAC_SIGNAL_PORT}"
        try:
            async with deps.websocket_connect(
                uri,
                subprotocols=protocols or None,
                open_timeout=5,
                close_timeout=2,
                max_size=None,
            ) as upstream:
                accepted = (
                    upstream.subprotocol if upstream.subprotocol in protocols else None
                )
                await websocket.accept(subprotocol=accepted)
                tasks = {
                    asyncio.create_task(
                        _relay_browser_to_upstream(websocket, upstream)
                    ),
                    asyncio.create_task(
                        _relay_upstream_to_browser(websocket, upstream)
                    ),
                }
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*done, *pending, return_exceptions=True)
        except Exception as exc:
            LOG.debug("LeIsaac signaling relay closed with an error", exc_info=exc)
            try:
                await websocket.close(code=1011)
            except Exception as close_exc:
                LOG.debug(
                    "LeIsaac browser WebSocket was already closed", exc_info=close_exc
                )
