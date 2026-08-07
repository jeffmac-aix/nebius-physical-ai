"""Bounded WebRTC data-channel relay for causal LeIsaac video frames.

Control and safety acknowledgements deliberately stay on the reliable ordered
WebSocket.  This module carries only independently decodable, sequence-stamped
video frames over an unordered partial-reliability SCTP channel so loss cannot
build a stale presentation queue.  The existing binary WebSocket remains the
fallback when WebRTC negotiation is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import AbstractAsyncContextManager
from typing import Any, Callable

try:  # agent VM: /opt/npa-agent is on sys.path
    from agent_backend.leisaac_transport import (
        AsyncLatestByKey,
        MAX_FRAME_BYTES,
        TransportMetrics,
        TransportProtocolError,
        VIDEO_SUBPROTOCOL,
        stamp_verified_frame,
        unpack_frame,
    )
except ImportError:  # repository tests
    from npa.agent_backend.leisaac_transport import (
        AsyncLatestByKey,
        MAX_FRAME_BYTES,
        TransportMetrics,
        TransportProtocolError,
        VIDEO_SUBPROTOCOL,
        stamp_verified_frame,
        unpack_frame,
    )


VIDEO_DATACHANNEL_LABEL = "npa-leisaac-video"
VIDEO_DATACHANNEL_PROTOCOL = "npa.leisaac.video.v1"
MAX_VIDEO_DATACHANNEL_OFFER_BYTES = 65_536
MAX_VIDEO_DATACHANNEL_FRAME_BYTES = 65_536
MAX_VIDEO_DATACHANNEL_PEERS = 4
VIDEO_DATACHANNEL_BUFFER_LOW_BYTES = 65_536


class VideoDataChannelError(RuntimeError):
    """A safe, client-facing WebRTC video negotiation failure."""


def parse_video_datachannel_offer(payload: Any, *, expected_run_id: str) -> str:
    """Validate one bounded, exact browser SDP offer payload."""

    if not isinstance(payload, dict) or set(payload) != {"v", "run_id", "type", "sdp"}:
        raise VideoDataChannelError("invalid WebRTC video offer")
    sdp = payload.get("sdp")
    if (
        payload.get("v") != 1
        or payload.get("type") != "offer"
        or payload.get("run_id") != expected_run_id
        or not isinstance(sdp, str)
        or not 1 <= len(sdp.encode("utf-8")) <= MAX_VIDEO_DATACHANNEL_OFFER_BYTES
        or "m=application" not in sdp
        or "UDP/DTLS/SCTP" not in sdp
    ):
        raise VideoDataChannelError("invalid WebRTC video offer")
    return sdp


def valid_video_datachannel(channel: Any) -> bool:
    """Require the browser's explicit stale-frame-dropping channel contract."""

    return (
        str(getattr(channel, "label", "")) == VIDEO_DATACHANNEL_LABEL
        and str(getattr(channel, "protocol", "")) == VIDEO_DATACHANNEL_PROTOCOL
        and getattr(channel, "ordered", True) is False
        and getattr(channel, "maxRetransmits", None) == 0
    )


class VideoDataChannelPeerPool:
    """Own a small bounded set of authenticated browser video peers."""

    def __init__(self, *, limit: int = MAX_VIDEO_DATACHANNEL_PEERS) -> None:
        if limit < 1 or limit > 16:
            raise ValueError("invalid WebRTC video peer limit")
        self.limit = limit
        self._peers: set[Any] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()

    @property
    def active(self) -> int:
        return len(self._peers)

    async def _discard(self, peer: Any) -> None:
        async with self._lock:
            self._peers.discard(peer)

    async def _close(self, peer: Any) -> None:
        try:
            await peer.close()
        finally:
            await self._discard(peer)

    async def create_answer(
        self,
        *,
        offer_sdp: str,
        run_id: str,
        ice_server: dict[str, Any],
        open_runtime: Callable[[], AbstractAsyncContextManager[Any]],
        metrics: TransportMetrics,
    ) -> dict[str, Any]:
        """Negotiate and retain one peer; its relay starts when the channel opens."""

        try:
            from aiortc import (  # type: ignore[import-not-found]
                RTCConfiguration,
                RTCIceServer,
                RTCPeerConnection,
                RTCSessionDescription,
            )
        except ImportError as exc:
            raise VideoDataChannelError("WebRTC video relay is unavailable") from exc

        urls = ice_server.get("urls")
        if not isinstance(urls, list) or len(urls) != 1:
            raise VideoDataChannelError("WebRTC relay configuration is unavailable")
        configuration = RTCConfiguration(
            iceServers=[
                RTCIceServer(
                    urls=[str(urls[0])],
                    username=str(ice_server.get("username") or ""),
                    credential=str(ice_server.get("credential") or ""),
                )
            ]
        )
        peer = RTCPeerConnection(configuration=configuration)
        async with self._lock:
            closed = {
                item
                for item in self._peers
                if str(getattr(item, "connectionState", "")) in {"closed", "failed"}
            }
            self._peers.difference_update(closed)
            if len(self._peers) >= self.limit:
                await peer.close()
                raise VideoDataChannelError("WebRTC video peer capacity is busy")
            self._peers.add(peer)

        loop = asyncio.get_running_loop()
        channel_ready: asyncio.Future[Any] = loop.create_future()

        @peer.on("datachannel")
        def on_datachannel(channel: Any) -> None:
            if channel_ready.done():
                channel.close()
                return
            if not valid_video_datachannel(channel):
                channel.close()
                channel_ready.set_exception(
                    VideoDataChannelError("invalid WebRTC video channel")
                )
                return
            channel.bufferedAmountLowThreshold = VIDEO_DATACHANNEL_BUFFER_LOW_BYTES
            channel_ready.set_result(channel)

        @peer.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            if str(peer.connectionState) in {"closed", "failed"}:
                await self._discard(peer)

        try:
            await peer.setRemoteDescription(
                RTCSessionDescription(sdp=offer_sdp, type="offer")
            )
            answer = await peer.createAnswer()
            await peer.setLocalDescription(answer)
            local = peer.localDescription
            if local is None or local.type != "answer" or not local.sdp:
                raise VideoDataChannelError("WebRTC video answer is unavailable")
        except Exception as exc:
            await self._close(peer)
            if isinstance(exc, VideoDataChannelError):
                raise
            raise VideoDataChannelError("WebRTC video negotiation failed") from exc

        task = asyncio.create_task(
            self._serve(
                peer=peer,
                channel_ready=channel_ready,
                open_runtime=open_runtime,
                run_id=run_id,
                metrics=metrics,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        metrics.increment("datachannel_connections")
        return {"v": 1, "type": "answer", "sdp": str(local.sdp)}

    async def _serve(
        self,
        *,
        peer: Any,
        channel_ready: asyncio.Future[Any],
        open_runtime: Callable[[], AbstractAsyncContextManager[Any]],
        run_id: str,
        metrics: TransportMetrics,
    ) -> None:
        latest = AsyncLatestByKey(("workspace", "overview"))
        try:
            channel = await asyncio.wait_for(channel_ready, timeout=10.0)
            async with open_runtime() as upstream:
                if upstream.subprotocol != VIDEO_SUBPROTOCOL:
                    raise TransportProtocolError(
                        "subprotocol", "runtime rejected the video subprotocol"
                    )

                async def read_runtime() -> None:
                    async for raw in upstream:
                        if not isinstance(raw, bytes) or len(raw) > MAX_FRAME_BYTES + 256:
                            raise TransportProtocolError(
                                "invalid_frame", "runtime video message is invalid"
                            )
                        envelope, content = await asyncio.to_thread(unpack_frame, raw)
                        await upstream.send(
                            json.dumps(
                                {
                                    "v": 1,
                                    "type": "frame-ack",
                                    "run_id": run_id,
                                    "sequence": envelope.sequence,
                                },
                                separators=(",", ":"),
                            )
                        )
                        metrics.increment("frames_relay_acked")
                        camera = "overview" if envelope.flags & 1 else "workspace"
                        await latest.publish(
                            camera, (envelope, content, time.monotonic_ns())
                        )

                async def send_browser() -> None:
                    generations: dict[str, int] = {}
                    next_camera_index = 0
                    while str(channel.readyState) == "open":
                        while (
                            str(channel.readyState) == "open"
                            and int(channel.bufferedAmount)
                            > VIDEO_DATACHANNEL_BUFFER_LOW_BYTES
                        ):
                            metrics.increment("datachannel_window_saturated")
                            await asyncio.sleep(0.002)
                        (
                            camera,
                            generation,
                            item,
                            skipped,
                            next_camera_index,
                        ) = await latest.wait_after(
                            generations,
                            next_index=next_camera_index,
                            timeout=20.0,
                        )
                        generations[camera] = generation
                        envelope, content, received_mono_ns = item
                        if skipped:
                            metrics.increment("frames_coalesced", skipped)
                        stamped = stamp_verified_frame(
                            envelope,
                            content,
                            received_mono_ns=received_mono_ns,
                            send_mono_ns=time.monotonic_ns(),
                            additional_dropped=skipped,
                        )
                        if len(stamped) > MAX_VIDEO_DATACHANNEL_FRAME_BYTES:
                            raise VideoDataChannelError(
                                "WebRTC video frame exceeds the bounded channel size"
                            )
                        channel.send(stamped)
                        metrics.increment("datachannel_frames_sent")

                tasks = {
                    asyncio.create_task(read_runtime()),
                    asyncio.create_task(send_browser()),
                }
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                results = await asyncio.gather(
                    *done, *pending, return_exceptions=True
                )
                for result in results:
                    if isinstance(result, Exception) and not isinstance(
                        result, asyncio.CancelledError
                    ):
                        raise result
        except asyncio.CancelledError:
            raise
        except Exception:
            metrics.increment("datachannel_errors")
        finally:
            await self._close(peer)
