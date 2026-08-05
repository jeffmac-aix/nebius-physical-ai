"""FastAPI routes for authenticated LeIsaac discovery and WebRTC signaling."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.websockets import WebSocket

LOG = logging.getLogger(__name__)
_BACKHAUL_HEADER_SIZE = 9
_BACKHAUL_MAX_FRAME = 4 * 1024 * 1024
_WS_SESSION_COOKIE = "npa_leisaac_ws"
_WS_SESSION_TTL_SECONDS = 120

try:  # agent VM: /opt/npa-agent is on sys.path
    from agent_backend.leisaac import (
        LEISAAC_CLIENT_MODULE_PATH,
        LEISAAC_CLIENT_JS_SHA256,
        LEISAAC_CONTROL_WS_PATH,
        LEISAAC_RECORDER_PATH,
        LEISAAC_SIGNAL_PORT,
        LEISAAC_VIDEO_WS_PATH,
        normalize_manifest,
        selected_run_id,
        status_payload,
        validate_health,
    )
    from agent_backend.leisaac_transport import (
        AsyncLatestValue,
        CONTROL_SUBPROTOCOL,
        MAX_CONTROL_MESSAGE_BYTES,
        MAX_FRAME_BYTES,
        TransportMetrics,
        TransportProtocolError,
        VIDEO_SUBPROTOCOL,
        parse_control_message,
        parse_video_ack,
        stamp_agent_frame,
        unpack_frame,
    )
except ImportError:  # repository tests
    from npa.agent_backend.leisaac import (
        LEISAAC_CLIENT_MODULE_PATH,
        LEISAAC_CLIENT_JS_SHA256,
        LEISAAC_CONTROL_WS_PATH,
        LEISAAC_RECORDER_PATH,
        LEISAAC_SIGNAL_PORT,
        LEISAAC_VIDEO_WS_PATH,
        normalize_manifest,
        selected_run_id,
        status_payload,
        validate_health,
    )
    from npa.agent_backend.leisaac_transport import (
        AsyncLatestValue,
        CONTROL_SUBPROTOCOL,
        MAX_CONTROL_MESSAGE_BYTES,
        MAX_FRAME_BYTES,
        TransportMetrics,
        TransportProtocolError,
        VIDEO_SUBPROTOCOL,
        parse_control_message,
        parse_video_ack,
        stamp_agent_frame,
        unpack_frame,
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


def _same_https_origin(headers: Any) -> bool:
    """Validate that an nginx-forwarded browser request has the public HTTPS origin."""

    if str(headers.get("x-forwarded-proto") or "").lower() != "https":
        return False
    origin = str(headers.get("origin") or "")
    host = str(headers.get("host") or "").lower()
    try:
        parsed = urlparse(origin)
        origin_host = str(parsed.hostname or "").lower()
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 0)
        host_name, _, raw_port = host.partition(":")
        host_port = int(raw_port) if raw_port else 443
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and origin_host == host_name
        and origin_port == host_port
    )


def _same_origin_session_request(headers: Any) -> bool:
    """Accept exact Origin or Chromium's same-origin Fetch Metadata + referrer."""

    if _same_https_origin(headers):
        return True
    if str(headers.get("sec-fetch-site") or "").lower() != "same-origin":
        return False
    forwarded = dict(headers)
    forwarded["origin"] = str(headers.get("referer") or "")
    return _same_https_origin(forwarded)


def _same_origin_websocket(websocket: WebSocket, subprotocol: str) -> bool:
    """Validate the public HTTPS origin and exact NPA subprotocol."""

    protocols = {
        item.strip()
        for item in str(websocket.headers.get("sec-websocket-protocol") or "").split(
            ","
        )
        if item.strip()
    }
    return _same_https_origin(websocket.headers) and protocols == {subprotocol}


def _client_address(headers: Any, client: Any) -> str:
    """Return the nginx-attested public client address without trusting browser input."""

    address = str(headers.get("x-real-ip") or "").strip()
    if address:
        return address[:128]
    return str(getattr(client, "host", "") or "")[:128]


def _mint_ws_session(
    secret: bytes,
    run_id: str,
    client_address: str,
    *,
    now: int | None = None,
) -> str:
    """Mint a short-lived opaque-to-the-browser transport authorization."""

    issued_at = int(time.time() if now is None else now)
    payload = json.dumps(
        {
            "client": client_address,
            "expires": issued_at + _WS_SESSION_TTL_SECONDS,
            "nonce": secrets.token_urlsafe(12),
            "run_id": run_id,
            "v": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    signature = (
        base64.urlsafe_b64encode(
            hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    return f"{body}.{signature}"


def _valid_ws_session(
    secret: bytes,
    token: str,
    run_id: str,
    client_address: str,
    *,
    now: int | None = None,
) -> bool:
    """Validate the signed run/address binding and its deliberately short lifetime."""

    try:
        body, signature = token.split(".", 1)
        expected = (
            base64.urlsafe_b64encode(
                hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        if not hmac.compare_digest(signature, expected):
            return False
        padding = "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body + padding))
        current = int(time.time() if now is None else now)
        expires = int(payload.get("expires", 0))
    except (
        UnicodeEncodeError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        binascii.Error,
    ):
        return False
    return (
        payload.get("v") == 1
        and payload.get("run_id") == run_id
        and payload.get("client") == client_address
        and isinstance(payload.get("nonce"), str)
        and 1 <= len(payload["nonce"]) <= 64
        and current <= expires <= current + _WS_SESSION_TTL_SECONDS + 5
    )


def _runtime_ws_uri(manifest: dict[str, Any], path: str) -> str:
    base = str(manifest["service_url"])
    if not base.startswith("http://"):
        raise ValueError("LeIsaac runtime service URL is not loopback HTTP")
    return "ws://" + base.removeprefix("http://").rstrip("/") + path


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
    transport_metrics = TransportMetrics()
    ws_session_secret = secrets.token_bytes(32)

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
        if payload.get("available"):
            payload["agent_transport_metrics"] = transport_metrics.snapshot()
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

    @app.post("/leisaac/ws-session")
    async def leisaac_ws_session(request: Request, run_id: str = "") -> Any:
        """Issue a short-lived HttpOnly credential for nginx-auth-free WS upgrades."""

        if (
            not _same_origin_session_request(request.headers)
            or request.headers.get("x-npa-leisaac-control") != "1"
        ):
            return deps.response(
                content=json.dumps(
                    {"detail": "same-origin authenticated HTTPS is required"}
                ),
                status_code=403,
                media_type="application/json",
                headers={"Cache-Control": "private, no-store"},
            )
        manifest, reason = await asyncio.to_thread(_resolve, deps, run_id)
        if not manifest:
            return deps.response(
                content=json.dumps({"detail": reason}),
                status_code=404,
                media_type="application/json",
                headers={"Cache-Control": "private, no-store"},
            )
        token = _mint_ws_session(
            ws_session_secret,
            str(manifest["run_id"]),
            _client_address(request.headers, request.client),
        )
        response = deps.response(
            content=b"",
            status_code=204,
            headers={"Cache-Control": "private, no-store"},
        )
        response.set_cookie(
            _WS_SESSION_COOKIE,
            token,
            max_age=_WS_SESSION_TTL_SECONDS,
            path="/api/leisaac/transport",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response

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
        response_headers = getattr(response, "headers", {})
        return deps.response(
            content=content,
            status_code=200,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                **{
                    header: str(response_headers.get(header))
                    for header in (
                        "X-NPA-Frame-Sequence",
                        "X-NPA-Frame-Capture-Wall-Ns",
                        "X-NPA-Frame-SHA256",
                    )
                    if response_headers.get(header)
                },
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
            content_length = int(request.headers.get("content-length") or "0")
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > MAX_CONTROL_MESSAGE_BYTES:
            return deps.response(
                content=json.dumps({"detail": "invalid LeIsaac input size"}),
                status_code=400,
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
        if isinstance(payload, dict) and payload.get("v") == 1:
            try:
                forwarded_payload = parse_control_message(
                    json.dumps(payload, separators=(",", ":")),
                    expected_run_id=str(manifest["run_id"]),
                )
            except TransportProtocolError as exc:
                return deps.response(
                    content=json.dumps(exc.payload()),
                    status_code=400,
                    media_type="application/json",
                )
        else:
            forwarded_payload = {"key": key, "event": event}
        if deps.http_post is None:
            upstream = None
        else:
            try:
                upstream = await asyncio.to_thread(
                    deps.http_post,
                    f"{manifest['service_url']}/input",
                    json=forwarded_payload,
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
        try:
            acknowledgement = upstream.json()
        except Exception:
            acknowledgement = {
                "detail": "LeIsaac control returned an invalid acknowledgement"
            }
        return deps.response(
            content=json.dumps(acknowledgement),
            status_code=202,
            media_type="application/json",
            headers={"Cache-Control": "private, no-store"},
        )

    @app.post(LEISAAC_RECORDER_PATH.removeprefix("/api"))
    async def leisaac_recorder(request: Request, run_id: str = "") -> Any:
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
        command = str(payload.get("command") if isinstance(payload, dict) else "")
        if command not in {"start", "mark-success", "mark-failure", "finalize"}:
            return deps.response(
                content=json.dumps({"detail": "invalid recorder command"}),
                status_code=400,
                media_type="application/json",
            )
        request_id = str(
            payload.get("request_id") if isinstance(payload, dict) else ""
        ) or secrets.token_hex(16)
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id):
            return deps.response(
                content=json.dumps({"detail": "invalid recorder request ID"}),
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
                    f"{manifest['service_url']}/recorder/control",
                    json={"command": command, "request_id": request_id},
                    headers={"X-NPA-LeIsaac-Nonce": manifest["session_nonce"]},
                    timeout=10.0,
                    follow_redirects=False,
                )
            except Exception:
                upstream = None
        if upstream is None:
            status_code = 503
            content = {"detail": "LeIsaac recorder is unavailable"}
        else:
            status_code = int(upstream.status_code)
            try:
                content = upstream.json()
            except Exception:
                content = {"detail": "LeIsaac recorder returned an invalid response"}
                status_code = 502
        if status_code not in {202, 400, 409, 503}:
            status_code = 502
            content = {"detail": "LeIsaac recorder returned an invalid status"}
        return deps.response(
            content=json.dumps(content),
            status_code=status_code,
            media_type="application/json",
            headers={"Cache-Control": "private, no-store"},
        )

    async def prepare_transport(
        websocket: WebSocket, subprotocol: str
    ) -> tuple[dict[str, Any] | None, str]:
        if not _same_origin_websocket(websocket, subprotocol):
            return None, "same-origin authenticated HTTPS WebSocket is required"
        if set(websocket.query_params.keys()) != {"run_id"}:
            return None, "only run_id is accepted"
        run_id = str(websocket.query_params.get("run_id") or "")
        if not _valid_ws_session(
            ws_session_secret,
            str(websocket.cookies.get(_WS_SESSION_COOKIE) or ""),
            run_id,
            _client_address(websocket.headers, websocket.client),
        ):
            return None, "valid short-lived transport session is required"
        manifest, reason = await asyncio.to_thread(_resolve, deps, run_id)
        if not manifest:
            return None, reason
        health, reason = await asyncio.to_thread(_health, deps, manifest)
        if not health:
            return None, reason
        if str(health.get("stream_transport") or "") != "websocket-v1":
            return None, "preferred transport is unavailable for this session"
        return manifest, ""

    @app.websocket(LEISAAC_CONTROL_WS_PATH.removeprefix("/api"))
    async def leisaac_transport_control(websocket: WebSocket) -> None:
        manifest, reason = await prepare_transport(websocket, CONTROL_SUBPROTOCOL)
        if not manifest:
            LOG.warning("LeIsaac control transport rejected: %s", reason)
            await websocket.close(code=1008)
            return
        run_id = str(manifest["run_id"])
        try:
            async with deps.websocket_connect(
                _runtime_ws_uri(manifest, "/transport/control"),
                subprotocols=[CONTROL_SUBPROTOCOL],
                additional_headers={
                    "X-NPA-LeIsaac-Nonce": manifest["session_nonce"],
                    "X-NPA-LeIsaac-Run-ID": run_id,
                },
                open_timeout=5,
                close_timeout=2,
                max_size=MAX_CONTROL_MESSAGE_BYTES,
                max_queue=4,
                ping_interval=10,
                ping_timeout=10,
                compression=None,
            ) as upstream:
                if upstream.subprotocol != CONTROL_SUBPROTOCOL:
                    raise TransportProtocolError(
                        "subprotocol", "runtime rejected the control subprotocol"
                    )
                await websocket.accept(subprotocol=CONTROL_SUBPROTOCOL)
                transport_metrics.increment("control_connections")

                async def browser_to_runtime() -> None:
                    while True:
                        message = await websocket.receive()
                        if message.get("type") == "websocket.disconnect":
                            return
                        raw = message.get("text")
                        if raw is None:
                            raise TransportProtocolError(
                                "invalid_message", "control messages must be text"
                            )
                        parsed = parse_control_message(raw, expected_run_id=run_id)
                        await upstream.send(json.dumps(parsed, separators=(",", ":")))

                async def runtime_to_browser() -> None:
                    async for raw in upstream:
                        if (
                            not isinstance(raw, str)
                            or len(raw.encode("utf-8")) > MAX_CONTROL_MESSAGE_BYTES
                        ):
                            raise TransportProtocolError(
                                "invalid_message",
                                "runtime control acknowledgement is invalid",
                            )
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError as exc:
                            raise TransportProtocolError(
                                "invalid_message", "runtime acknowledgement is not JSON"
                            ) from exc
                        if (
                            not isinstance(payload, dict)
                            or str(payload.get("run_id") or run_id) != run_id
                        ):
                            raise TransportProtocolError(
                                "run_mismatch", "runtime acknowledgement run mismatch"
                            )
                        payload["agent_received_mono_ns"] = str(time.monotonic_ns())
                        payload["agent_send_mono_ns"] = str(time.monotonic_ns())
                        await asyncio.wait_for(
                            websocket.send_text(
                                json.dumps(payload, separators=(",", ":"))
                            ),
                            timeout=2.0,
                        )

                tasks = {
                    asyncio.create_task(browser_to_runtime()),
                    asyncio.create_task(runtime_to_browser()),
                }
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                results = await asyncio.gather(*done, *pending, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception) and not isinstance(
                        result, asyncio.CancelledError
                    ):
                        raise result
        except TransportProtocolError as exc:
            transport_metrics.increment("control_errors")
            try:
                await websocket.send_text(
                    json.dumps(exc.payload(), separators=(",", ":"))
                )
            except Exception as send_exc:
                LOG.debug(
                    "LeIsaac control protocol error could not be returned",
                    exc_info=send_exc,
                )
            try:
                await websocket.close(code=1008)
            except Exception as close_exc:
                LOG.debug(
                    "LeIsaac control WebSocket was already closed",
                    exc_info=close_exc,
                )
        except Exception as exc:
            LOG.warning("LeIsaac control transport closed: %s", type(exc).__name__)
            try:
                await websocket.close(code=1013)
            except Exception as close_exc:
                LOG.debug(
                    "LeIsaac control WebSocket was already closed",
                    exc_info=close_exc,
                )

    @app.websocket(LEISAAC_VIDEO_WS_PATH.removeprefix("/api"))
    async def leisaac_transport_video(websocket: WebSocket) -> None:
        manifest, reason = await prepare_transport(websocket, VIDEO_SUBPROTOCOL)
        if not manifest:
            LOG.warning("LeIsaac video transport rejected: %s", reason)
            await websocket.close(code=1008)
            return
        run_id = str(manifest["run_id"])
        latest = AsyncLatestValue()
        try:
            async with deps.websocket_connect(
                _runtime_ws_uri(manifest, "/transport/video"),
                subprotocols=[VIDEO_SUBPROTOCOL],
                additional_headers={
                    "X-NPA-LeIsaac-Nonce": manifest["session_nonce"],
                    "X-NPA-LeIsaac-Run-ID": run_id,
                },
                open_timeout=5,
                close_timeout=2,
                max_size=MAX_FRAME_BYTES + 256,
                max_queue=2,
                ping_interval=10,
                ping_timeout=10,
                compression=None,
            ) as upstream:
                if upstream.subprotocol != VIDEO_SUBPROTOCOL:
                    raise TransportProtocolError(
                        "subprotocol", "runtime rejected the video subprotocol"
                    )
                await websocket.accept(subprotocol=VIDEO_SUBPROTOCOL)
                transport_metrics.increment("video_connections")

                async def read_runtime() -> None:
                    async for raw in upstream:
                        if (
                            not isinstance(raw, bytes)
                            or len(raw) > MAX_FRAME_BYTES + 256
                        ):
                            raise TransportProtocolError(
                                "invalid_frame", "runtime video message is invalid"
                            )
                        unpack_frame(raw)
                        await latest.publish((raw, time.monotonic_ns()))

                async def acknowledge_runtime() -> None:
                    while True:
                        message = await websocket.receive()
                        if message.get("type") == "websocket.disconnect":
                            return
                        raw = message.get("text")
                        if raw is None:
                            raise TransportProtocolError(
                                "invalid_message",
                                "video acknowledgements must be text",
                            )
                        acknowledgement = parse_video_ack(raw, expected_run_id=run_id)
                        await upstream.send(
                            json.dumps(acknowledgement, separators=(",", ":"))
                        )

                async def send_browser() -> None:
                    generation = 0
                    while True:
                        generation, item, skipped = await latest.wait_after(
                            generation, timeout=20.0
                        )
                        raw, received_mono_ns = item
                        if skipped:
                            transport_metrics.increment("frames_coalesced", skipped)
                        stamped = stamp_agent_frame(
                            raw,
                            received_mono_ns=received_mono_ns,
                            send_mono_ns=time.monotonic_ns(),
                            additional_dropped=skipped,
                        )
                        try:
                            await asyncio.wait_for(
                                websocket.send_bytes(stamped), timeout=2.0
                            )
                        except asyncio.TimeoutError:
                            transport_metrics.increment("slow_client_disconnects")
                            raise
                        transport_metrics.increment("frames_sent")

                tasks = {
                    asyncio.create_task(read_runtime()),
                    asyncio.create_task(acknowledge_runtime()),
                    asyncio.create_task(send_browser()),
                }
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                results = await asyncio.gather(*done, *pending, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception) and not isinstance(
                        result, asyncio.CancelledError
                    ):
                        raise result
        except Exception as exc:
            LOG.warning("LeIsaac video transport closed: %s", type(exc).__name__)
            try:
                await websocket.close(code=1013)
            except Exception as close_exc:
                LOG.debug(
                    "LeIsaac video WebSocket was already closed",
                    exc_info=close_exc,
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
