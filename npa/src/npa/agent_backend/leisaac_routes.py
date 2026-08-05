"""FastAPI routes for authenticated LeIsaac discovery and WebRTC signaling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from starlette.requests import Request
from starlette.websockets import WebSocket

LOG = logging.getLogger(__name__)
_BACKHAUL_HEADER_SIZE = 9
_BACKHAUL_MAX_FRAME = 4 * 1024 * 1024

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
    http_post: Callable[..., Any] | None = None
    save_state: Callable[[dict], None] | None = None


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

    manifest_cache: dict[str, tuple[float, dict | None, str]] = {}
    manifest_cache_lock = threading.Lock()

    def cached_resolve(run_id: str) -> tuple[dict | None, str]:
        """Bound high-rate frame polling to one capability lookup per five seconds."""

        now = time.monotonic()
        with manifest_cache_lock:
            cached = manifest_cache.get(run_id)
            if cached is not None and now - cached[0] < 5.0:
                return cached[1], cached[2]
        manifest, reason = _resolve(deps, run_id)
        with manifest_cache_lock:
            manifest_cache[run_id] = (now, manifest, reason)
        return manifest, reason

    @app.get("/leisaac/status")
    def leisaac_status(request: Request, run_id: str = "") -> Any:
        if str(request.headers.get("x-forwarded-proto") or "").lower() != "https":
            payload = status_payload(
                None,
                reason="LeIsaac teleoperation requires the public HTTPS agent endpoint.",
            )
        else:
            manifest, reason = _resolve(deps, run_id)
            if not manifest:
                payload = status_payload(None, reason=reason)
            else:
                health, reason = _health(deps, manifest)
                payload = status_payload(manifest, health, reason=reason)
        return deps.response(
            content=json.dumps(payload),
            status_code=200,
            media_type="application/json",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/leisaac/select")
    async def leisaac_select(request: Request) -> Any:
        if (
            str(request.headers.get("x-forwarded-proto") or "").lower() != "https"
            or request.headers.get("x-npa-leisaac-control") != "1"
        ):
            return deps.response(
                content=json.dumps(
                    {"detail": "authenticated HTTPS capability selection is required"}
                ),
                status_code=403,
                media_type="application/json",
            )
        try:
            body = await request.json()
        except ValueError:
            body = None
        run_id = str(body.get("run_id") if isinstance(body, dict) else "")
        manifest, reason = _resolve(deps, run_id)
        if not manifest:
            return deps.response(
                content=json.dumps({"detail": reason}),
                status_code=404,
                media_type="application/json",
            )
        health, reason = await asyncio.to_thread(_health, deps, manifest)
        if not health:
            return deps.response(
                content=json.dumps({"detail": reason}),
                status_code=503,
                media_type="application/json",
            )
        if deps.save_state is None:
            return deps.response(
                content=json.dumps({"detail": "LeIsaac selection is unavailable"}),
                status_code=503,
                media_type="application/json",
            )
        state = deps.load_state()
        if not isinstance(state, dict):
            state = {}
        state["leisaac"] = {"run_id": manifest["run_id"]}
        deps.save_state(state)
        return deps.response(
            content=json.dumps(
                {
                    "selected": True,
                    "run_id": manifest["run_id"],
                    "available": True,
                }
            ),
            status_code=200,
            media_type="application/json",
            headers={"Cache-Control": "private, no-store"},
        )

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

    @app.get("/leisaac/frame.jpg")
    def leisaac_frame(request: Request, run_id: str = "") -> Any:
        if str(request.headers.get("x-forwarded-proto") or "").lower() != "https":
            return deps.response(
                content=json.dumps({"detail": "public HTTPS is required"}),
                status_code=400,
                media_type="application/json",
            )
        manifest, reason = cached_resolve(run_id)
        if not manifest:
            return deps.response(
                content=json.dumps({"detail": reason}),
                status_code=404,
                media_type="application/json",
            )
        try:
            response = deps.http_get(
                f"{manifest['service_url']}/frame.jpg",
                timeout=5.0,
                follow_redirects=False,
                headers={"X-NPA-LeIsaac-Nonce": manifest["session_nonce"]},
            )
        except Exception:
            response = None
        if response is None or int(response.status_code) != 200:
            return deps.response(
                content=json.dumps({"detail": "LeIsaac frame is unavailable"}),
                status_code=503,
                media_type="application/json",
            )
        content = bytes(response.content)
        if (
            not content.startswith(b"\xff\xd8")
            or not content.endswith(b"\xff\xd9")
            or len(content) > 4 * 1024 * 1024
        ):
            return deps.response(
                content=json.dumps({"detail": "LeIsaac frame failed validation"}),
                status_code=502,
                media_type="application/json",
            )
        return deps.response(
            content=content,
            status_code=200,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/leisaac/input")
    async def leisaac_input(request: Request, run_id: str = "") -> Any:
        if (
            str(request.headers.get("x-forwarded-proto") or "").lower() != "https"
            or request.headers.get("x-npa-leisaac-control") != "1"
        ):
            return deps.response(
                content=json.dumps(
                    {"detail": "authenticated HTTPS control is required"}
                ),
                status_code=403,
                media_type="application/json",
            )
        try:
            payload = await request.json()
        except ValueError:
            payload = None
        key = str(payload.get("key") if isinstance(payload, dict) else "").upper()
        event = str(payload.get("event") if isinstance(payload, dict) else "")
        if key not in {
            "W",
            "S",
            "A",
            "D",
            "Q",
            "E",
            "J",
            "L",
            "I",
            "K",
            "U",
            "O",
            "R",
            "N",
        } or event not in {"press", "release"}:
            return deps.response(
                content=json.dumps({"detail": "invalid LeIsaac input"}),
                status_code=400,
                media_type="application/json",
            )
        manifest, reason = await asyncio.to_thread(cached_resolve, run_id)
        if not manifest:
            return deps.response(
                content=json.dumps({"detail": reason}),
                status_code=404,
                media_type="application/json",
            )
        if deps.http_post is None:
            upstream = None
        else:
            try:
                upstream = await asyncio.to_thread(
                    deps.http_post,
                    f"{manifest['service_url']}/input",
                    json={"key": key, "event": event},
                    headers={"X-NPA-LeIsaac-Nonce": manifest["session_nonce"]},
                    timeout=5.0,
                    follow_redirects=False,
                )
            except Exception:
                upstream = None
        if upstream is None or int(upstream.status_code) != 202:
            return deps.response(
                content=json.dumps({"detail": "LeIsaac control is unavailable"}),
                status_code=503,
                media_type="application/json",
            )
        return deps.response(
            content=json.dumps({"accepted": True}),
            status_code=202,
            media_type="application/json",
            headers={"Cache-Control": "private, no-store"},
        )

    @app.websocket("/leisaac/signal")
    @app.websocket("/leisaac/signal/{signal_path:path}")
    async def leisaac_signal(websocket: WebSocket, signal_path: str = "") -> None:
        if str(websocket.headers.get("x-forwarded-proto") or "").lower() != "https":
            LOG.warning("LeIsaac signaling rejected: public HTTPS was not preserved")
            await websocket.close(code=1008)
            return
        # Isaac Sim's 5.1 browser client opens its signaling WebSocket at
        # ``<configured-path>/sign_in``.  Keep the bare path for protocol
        # compatibility tests, but do not turn this into an arbitrary upstream
        # path proxy.
        if signal_path not in ("", "sign_in"):
            LOG.warning("LeIsaac signaling rejected: unsupported upstream path")
            await websocket.close(code=1008)
            return
        run_id = str(websocket.query_params.get("run_id") or "")
        # Storage discovery and the loopback health request are synchronous.
        # In agent-relay mode their response also traverses the backhaul route
        # on this ASGI event loop, so running either call inline can deadlock
        # the WebSocket that is needed to return its own response.
        manifest, reason = await asyncio.to_thread(_resolve, deps, run_id)
        if not manifest:
            LOG.warning("LeIsaac signaling rejected: %s", reason)
            await websocket.close(code=1008)
            return
        health, reason = await asyncio.to_thread(_health, deps, manifest)
        if not health:
            LOG.warning("LeIsaac signaling rejected: %s", reason)
            await websocket.close(code=1013)
            return

        requested = str(websocket.headers.get("sec-websocket-protocol") or "")
        protocols = [item.strip() for item in requested.split(",") if item.strip()]
        protocols = [
            item for item in protocols if len(item) <= 128 and "\n" not in item
        ]
        query = str(websocket.url.query or "")
        if len(query) > 4096 or any(char in query for char in "\r\n"):
            await websocket.close(code=1008)
            return
        upstream_path = f"/{signal_path}" if signal_path else ""
        uri = f"ws://{manifest['signal_host']}:{LEISAAC_SIGNAL_PORT}{upstream_path}"
        if query:
            uri += f"?{query}"
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
            LOG.warning(
                "LeIsaac signaling upstream connection failed: %s",
                type(exc).__name__,
                exc_info=exc,
            )
            try:
                await websocket.close(code=1011)
            except Exception as close_exc:
                LOG.debug(
                    "LeIsaac browser WebSocket was already closed", exc_info=close_exc
                )

    @app.websocket("/leisaac/backhaul")
    async def leisaac_backhaul(websocket: WebSocket) -> None:
        """Bridge the authenticated pod WSS backhaul to the loopback relay."""

        # This route is reachable only through nginx's exact authenticated WSS
        # location; the backend listener itself is loopback-only.
        await websocket.accept()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 48081)
        except OSError:
            await websocket.close(code=1013)
            return

        async def websocket_to_relay() -> None:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                payload = message.get("bytes")
                if (
                    payload is None
                    or len(payload) > _BACKHAUL_MAX_FRAME + _BACKHAUL_HEADER_SIZE
                ):
                    raise ValueError("invalid LeIsaac backhaul frame")
                writer.write(payload)
                await writer.drain()

        async def relay_to_websocket() -> None:
            while True:
                header = await reader.readexactly(_BACKHAUL_HEADER_SIZE)
                size = int.from_bytes(header[5:9], "big")
                if size > _BACKHAUL_MAX_FRAME:
                    raise ValueError("invalid LeIsaac backhaul frame")
                await websocket.send_bytes(header + await reader.readexactly(size))

        tasks = {
            asyncio.create_task(websocket_to_relay()),
            asyncio.create_task(relay_to_websocket()),
        }
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        finally:
            writer.close()
            await writer.wait_closed()
