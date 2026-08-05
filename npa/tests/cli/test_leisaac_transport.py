"""Deterministic protocol and runtime tests for low-latency LeIsaac transport."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from npa.agent_backend.leisaac_routes import (
    _mint_ws_session,
    _same_origin_websocket,
    _valid_ws_session,
)
from npa.agent_backend.leisaac_transport import (
    AsyncLatestValue,
    CONTROL_SUBPROTOCOL,
    ControlLedger,
    FrameEnvelope,
    MAX_CONTROL_MESSAGE_BYTES,
    TransportMetrics,
    TransportProtocolError,
    VIDEO_SUBPROTOCOL,
    pack_frame,
    parse_control_message,
    parse_video_ack,
    stamp_agent_frame,
    unpack_frame,
)

ROOT = Path(__file__).resolve().parents[3]
RUN_ID = "leisaac-transport-test"
NONCE = "n" * 64


def _control(seq: int = 1, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "v": 1,
        "type": "control",
        "run_id": RUN_ID,
        "client_id": "browser-test",
        "seq": seq,
        "key": "W",
        "event": "press",
        "client_mono_ns": 100 + seq,
        "client_wall_ns": 200 + seq,
    }
    payload.update(overrides)
    return payload


def _runtime_module():
    path = ROOT / "npa/docker/workbench/leisaac/session_server.py"
    spec = importlib.util.spec_from_file_location(
        f"npa_leisaac_transport_{id(path)}", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_control_messages_are_bounded_and_exactly_scoped() -> None:
    parsed = parse_control_message(json.dumps(_control()), expected_run_id=RUN_ID)
    assert parsed["key"] == "W"
    assert parsed["seq"] == 1

    for override, code in (
        ({"run_id": "other"}, "run_mismatch"),
        ({"key": "R"}, "invalid_message"),
        ({"seq": -1}, "invalid_message"),
        ({"type": "unknown"}, "invalid_message"),
    ):
        with pytest.raises(TransportProtocolError) as exc_info:
            parse_control_message(
                json.dumps(_control(**override)), expected_run_id=RUN_ID
            )
        assert exc_info.value.code == code

    with pytest.raises(TransportProtocolError, match="size"):
        parse_control_message(
            b"{" + b"x" * MAX_CONTROL_MESSAGE_BYTES, expected_run_id=RUN_ID
        )


def test_control_ledger_is_ordered_idempotent_and_recovers_state() -> None:
    ledger = ControlLedger(history_limit=2)
    first = _control()
    accepted, queued = ledger.accept(first, received_mono_ns=301, received_wall_ns=401)
    assert accepted["phase"] == "accepted"
    assert queued is not None and queued["seq"] == 1
    assert ledger.keys_down("browser-test") == ("W",)

    duplicate, duplicate_queue = ledger.accept(first)
    assert duplicate["duplicate"] is True
    assert duplicate_queue is None

    with pytest.raises(TransportProtocolError) as reused:
        ledger.accept(_control(event="release"))
    assert reused.value.code == "sequence_reused"
    with pytest.raises(TransportProtocolError) as gap:
        ledger.accept(_control(3))
    assert gap.value.code == "out_of_order"
    assert gap.value.expected_seq == 2

    applied = {
        "client_id": "browser-test",
        "seq": 1,
        "simulator_applied_mono_ns": "501",
        "simulator_applied_wall_ns": "601",
        "simulator_step": 7,
    }
    assert ledger.mark_applied(applied) == applied
    assert ledger.applied("browser-test", 1) == applied
    resume = ledger.resume("browser-test")
    assert resume["next_seq"] == 2
    assert resume["last_applied_seq"] == 1
    assert resume["keys_down"] == ["W"]

    ledger.accept(_control(2, event="release"))
    ledger.accept(_control(3, key="A"))
    assert ledger.keys_down("browser-test") == ("A",)
    with pytest.raises(TransportProtocolError) as stale:
        ledger.accept(first)
    assert stale.value.code == "sequence_too_old"


def test_binary_frame_envelope_round_trips_and_detects_tampering() -> None:
    jpeg = b"\xff\xd8" + b"frame-data" * 20 + b"\xff\xd9"
    envelope = FrameEnvelope(
        sequence=9,
        capture_wall_ns=100,
        capture_monotonic_ns=101,
        encoded_wall_ns=102,
        encoded_monotonic_ns=103,
        runtime_send_monotonic_ns=104,
        dropped_before=2,
    )
    packed = pack_frame(envelope, jpeg)
    decoded, content = unpack_frame(packed)
    assert content == jpeg
    assert decoded.sequence == 9
    assert decoded.dropped_before == 2
    assert decoded.sha256 == hashlib.sha256(jpeg).digest()

    stamped = stamp_agent_frame(
        packed, received_mono_ns=105, send_mono_ns=106, additional_dropped=3
    )
    decoded, _content = unpack_frame(stamped)
    assert decoded.agent_receive_monotonic_ns == 105
    assert decoded.agent_send_monotonic_ns == 106
    assert decoded.dropped_before == 5

    tampered = bytearray(stamped)
    tampered[-2] ^= 0x01
    with pytest.raises(TransportProtocolError, match="digest"):
        unpack_frame(bytes(tampered))


def test_video_paint_ack_is_bounded_exact_and_run_scoped() -> None:
    acknowledgement = parse_video_ack(
        json.dumps({"v": 1, "type": "frame-ack", "run_id": RUN_ID, "sequence": 17}),
        expected_run_id=RUN_ID,
    )
    assert acknowledgement["sequence"] == 17

    with pytest.raises(TransportProtocolError, match="run ID"):
        parse_video_ack(
            json.dumps(
                {"v": 1, "type": "frame-ack", "run_id": "other", "sequence": 17}
            ),
            expected_run_id=RUN_ID,
        )
    with pytest.raises(TransportProtocolError, match="invalid video"):
        parse_video_ack(
            json.dumps(
                {
                    "v": 1,
                    "type": "frame-ack",
                    "run_id": RUN_ID,
                    "sequence": 17,
                    "unexpected": True,
                }
            ),
            expected_run_id=RUN_ID,
        )
    with pytest.raises(TransportProtocolError, match="size"):
        parse_video_ack("x" * 513, expected_run_id=RUN_ID)


@pytest.mark.anyio
async def test_latest_frame_wins_for_a_slow_consumer() -> None:
    latest = AsyncLatestValue()
    await latest.publish("frame-1")
    await latest.publish("frame-2")
    generation, value, skipped = await latest.wait_after(0, timeout=0.1)
    assert (generation, value, skipped) == (2, "frame-2", 1)

    waiter = asyncio.create_task(latest.wait_after(generation, timeout=0.1))
    await asyncio.sleep(0)
    await latest.publish("frame-3")
    assert await waiter == (3, "frame-3", 0)
    with pytest.raises(asyncio.TimeoutError):
        await latest.wait_after(3, timeout=0.001)


def test_transport_metrics_are_low_cardinality() -> None:
    metrics = TransportMetrics()
    metrics.increment("frames_sent", 2)
    assert metrics.snapshot()["frames_sent"] == 2
    with pytest.raises(ValueError):
        metrics.increment("run-id-as-a-label")


@pytest.mark.parametrize(
    "headers,allowed",
    [
        (
            {
                "x-forwarded-proto": "https",
                "origin": "https://agent.example",
                "host": "agent.example",
                "sec-websocket-protocol": CONTROL_SUBPROTOCOL,
            },
            True,
        ),
        (
            {
                "x-forwarded-proto": "https",
                "origin": "https://evil.example",
                "host": "agent.example",
                "sec-websocket-protocol": CONTROL_SUBPROTOCOL,
            },
            False,
        ),
        (
            {
                "x-forwarded-proto": "http",
                "origin": "https://agent.example",
                "host": "agent.example",
                "sec-websocket-protocol": CONTROL_SUBPROTOCOL,
            },
            False,
        ),
        (
            {
                "x-forwarded-proto": "https",
                "origin": "https://agent.example",
                "host": "agent.example",
                "sec-websocket-protocol": f"{CONTROL_SUBPROTOCOL}, extra",
            },
            False,
        ),
    ],
)
def test_public_websocket_requires_exact_origin_and_subprotocol(
    headers, allowed
) -> None:
    websocket = SimpleNamespace(headers=headers)
    assert _same_origin_websocket(websocket, CONTROL_SUBPROTOCOL) is allowed


def test_short_lived_ws_session_is_signed_and_bound_to_run_address_and_time() -> None:
    secret = b"deterministic-test-secret"
    token = _mint_ws_session(secret, RUN_ID, "203.0.113.7", now=1_000)

    assert _valid_ws_session(secret, token, RUN_ID, "203.0.113.7", now=1_000)
    assert _valid_ws_session(secret, token, RUN_ID, "203.0.113.7", now=1_120)
    assert not _valid_ws_session(secret, token, "other-run", "203.0.113.7", now=1_000)
    assert not _valid_ws_session(secret, token, RUN_ID, "203.0.113.8", now=1_000)
    assert not _valid_ws_session(secret, token, RUN_ID, "203.0.113.7", now=1_121)
    assert not _valid_ws_session(
        secret,
        token[:-1] + ("A" if token[-1] != "A" else "B"),
        RUN_ID,
        "203.0.113.7",
        now=1_000,
    )


def _prepare_runtime(monkeypatch, tmp_path: Path):
    runtime = _runtime_module()
    paths = {
        "INPUT_COUNTER_PATH": tmp_path / "input-count",
        "APPLIED_COUNTER_PATH": tmp_path / "applied-count",
        "INPUT_QUEUE_PATH": tmp_path / "input.jsonl",
        "FRAME_PATH": tmp_path / "frame.jpg",
        "FRAME_META_PATH": tmp_path / "frame.json",
        "APPLIED_ACK_PATH": tmp_path / "applied.jsonl",
        "RECORDER_ROOT": tmp_path / "recorder",
        "RECORDER_STATUS_PATH": tmp_path / "recorder/status.json",
        "RECORDER_CONTROL_PATH": tmp_path / "recorder/control.jsonl",
        "RECORDER_PENDING_PATH": tmp_path / "recorder/pending.json",
    }
    for name, path in paths.items():
        monkeypatch.setattr(runtime, name, path)
    monkeypatch.setenv("NPA_LEISAAC_RUN_ID", RUN_ID)
    monkeypatch.setenv("NPA_LEISAAC_SESSION_NONCE", NONCE)
    runtime.STATE.update(state="ready", detail="ready", webrtc_ready=True, pid=123)
    runtime.CONTROL_LEDGER = ControlLedger()
    runtime.TRANSPORT_METRICS = TransportMetrics()
    runtime.FRAME_LATEST = AsyncLatestValue()
    runtime.APPLIED_ACK_OFFSET = 0
    return runtime


def _runtime_headers() -> dict[str, str]:
    return {
        "x-npa-leisaac-nonce": NONCE,
        "x-npa-leisaac-run-id": RUN_ID,
    }


def test_runtime_control_ack_ordering_application_and_disconnect_cleanup(
    monkeypatch, tmp_path: Path
) -> None:
    runtime = _prepare_runtime(monkeypatch, tmp_path)
    with TestClient(runtime.build_app()) as client:
        with client.websocket_connect(
            "/transport/control",
            headers=_runtime_headers(),
            subprotocols=[CONTROL_SUBPROTOCOL],
        ) as websocket:
            websocket.send_json(
                {
                    "v": 1,
                    "type": "resume",
                    "run_id": RUN_ID,
                    "client_id": "browser-test",
                    "last_acked_seq": 0,
                    "keys_down": [],
                    "client_mono_ns": 1,
                    "client_wall_ns": 2,
                }
            )
            assert websocket.receive_json()["next_seq"] == 1

            websocket.send_json(_control(2))
            error = websocket.receive_json()
            assert error["code"] == "out_of_order"
            assert error["expected_seq"] == 1

            websocket.send_json(_control())
            accepted = websocket.receive_json()
            assert accepted["phase"] == "accepted"
            record = json.loads(runtime.INPUT_QUEUE_PATH.read_text().splitlines()[0])
            runtime.APPLIED_ACK_PATH.write_text(
                json.dumps(
                    {
                        **record,
                        "simulator_applied_mono_ns": "700",
                        "simulator_applied_wall_ns": "800",
                        "simulator_step": 9,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            applied = websocket.receive_json()
            assert applied["phase"] == "applied"
            assert applied["seq"] == 1
            assert applied["simulator_step"] == 9

            websocket.send_json(_control())
            assert websocket.receive_json()["duplicate"] is True
            assert websocket.receive_json()["phase"] == "applied"

    deadline = time.monotonic() + 1
    records = []
    while time.monotonic() < deadline:
        records = [
            json.loads(line)
            for line in runtime.INPUT_QUEUE_PATH.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        if len(records) >= 2:
            break
        time.sleep(0.005)
    assert [(item["seq"], item["event"]) for item in records] == [
        (1, "press"),
        (2, "release"),
    ]


def test_runtime_rejects_bad_auth_and_preserves_polling_fallback(
    monkeypatch, tmp_path: Path
) -> None:
    runtime = _prepare_runtime(monkeypatch, tmp_path)
    with TestClient(runtime.build_app()) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/transport/control",
                headers={
                    "x-npa-leisaac-nonce": "wrong",
                    "x-npa-leisaac-run-id": RUN_ID,
                },
                subprotocols=[CONTROL_SUBPROTOCOL],
            ):
                pass
        assert exc_info.value.code == 1008

        fallback = client.post(
            "/input",
            headers={"x-npa-leisaac-nonce": NONCE},
            json={"key": "A", "event": "press"},
        )
        assert fallback.status_code == 202
        assert fallback.json()["phase"] == "accepted"
        assert json.loads(runtime.INPUT_QUEUE_PATH.read_text())["key"] == "A"


def test_runtime_video_envelope_is_binary_and_nonblank(
    monkeypatch, tmp_path: Path
) -> None:
    runtime = _prepare_runtime(monkeypatch, tmp_path)
    jpeg = b"\xff\xd8" + b"real-frame" * 30 + b"\xff\xd9"
    runtime.FRAME_PATH.write_bytes(jpeg)
    runtime.FRAME_META_PATH.write_text(
        json.dumps(
            {
                "schema": "npa.leisaac.frame.v1",
                "sequence": 4,
                "capture_wall_ns": 100,
                "capture_monotonic_ns": 101,
                "encoded_wall_ns": 102,
                "encoded_monotonic_ns": 103,
                "bytes": len(jpeg),
                "sha256": hashlib.sha256(jpeg).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    with TestClient(runtime.build_app()) as client:
        with client.websocket_connect(
            "/transport/video",
            headers=_runtime_headers(),
            subprotocols=[VIDEO_SUBPROTOCOL],
        ) as websocket:
            envelope, content = unpack_frame(websocket.receive_bytes())
            assert envelope.sequence == 4
            assert envelope.runtime_send_monotonic_ns > 0
            assert content == jpeg
            next_jpeg = b"\xff\xd8" + b"new-frame" * 30 + b"\xff\xd9"
            runtime.FRAME_PATH.write_bytes(next_jpeg)
            runtime.FRAME_META_PATH.write_text(
                json.dumps(
                    {
                        "schema": "npa.leisaac.frame.v1",
                        "sequence": 5,
                        "capture_wall_ns": 200,
                        "capture_monotonic_ns": 201,
                        "encoded_wall_ns": 202,
                        "encoded_monotonic_ns": 203,
                        "bytes": len(next_jpeg),
                        "sha256": hashlib.sha256(next_jpeg).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            websocket.send_json(
                {"v": 1, "type": "frame-ack", "run_id": RUN_ID, "sequence": 4}
            )
            next_envelope, next_content = unpack_frame(websocket.receive_bytes())
            assert next_envelope.sequence == 5
            assert next_content == next_jpeg
