from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from teleop.app import create_app
from teleop.config import TeleopConfig


async def receive_type(ws, message_type: str, timeout: float = 3.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        message = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
        if message.get("type") == message_type:
            return message
    raise AssertionError(f"did not receive message type {message_type}")


async def receive_state(ws, state: str, timeout: float = 3.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        remaining = deadline - asyncio.get_running_loop().time()
        message = await asyncio.wait_for(ws.receive_json(), timeout=remaining)
        if message.get("type") == "status" and message.get("state") == state:
            return message
    raise AssertionError(f"did not receive worker state {state}")


async def receive_status(ws, predicate, timeout: float = 3.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        remaining = deadline - asyncio.get_running_loop().time()
        message = await asyncio.wait_for(ws.receive_json(), timeout=remaining)
        if message.get("type") == "status" and predicate(message):
            return message
    raise AssertionError("did not receive expected worker status")


async def test_websocket_claim_pose_and_release_flow() -> None:
    root = Path(__file__).resolve().parents[2]
    config = TeleopConfig(
        web_dir=root / "web",
        tls_cert_path=None,
        tls_key_path=None,
        status_hz=20.0,
    )
    async with TestServer(create_app(config)) as server:
        async with TestClient(server) as client:
            live = await client.get("/health/live")
            assert live.status == 200
            monitor = await client.get("/monitor")
            assert monitor.status == 200
            assert "VR Teleop 현장 모니터" in await monitor.text()

            ws = await client.ws_connect("/ws")
            hello = await receive_type(ws, "hello")
            session_id = hello["session_id"]
            await ws.send_json(
                {
                    "version": 1,
                    "type": "event",
                    "session_id": session_id,
                    "event": "claim_control",
                }
            )
            control = await receive_type(ws, "control")
            assert control["granted"] is True
            idle = await receive_state(ws, "IDLE")
            assert idle["gripper_enabled"] is False
            assert idle["gripper_busy"] is False
            assert idle["gripper_position"] == 0

            await ws.send_json(
                {
                    "version": 1,
                    "type": "telemetry",
                    "session_id": session_id,
                    "seq": 0,
                    "client_time_ms": 1000.0,
                    "xr_frame_count": 90,
                    "valid_pose_count": 89,
                    "pose_send_count": 89,
                    "pose_drop_count": 0,
                    "tracking_loss_count": 1,
                    "max_xr_frame_gap_ms": 14.0,
                    "max_pose_gap_ms": 28.0,
                    "ws_buffered_amount": 0,
                    "last_rtt_ms": 5.0,
                    "max_rtt_ms": 7.5,
                }
            )
            telemetry_ack = await receive_type(ws, "telemetry_ack")
            assert telemetry_ack["seq"] == 0
            telemetry_status = await receive_status(
                ws,
                lambda status: (
                    status.get("client_telemetry", {}).get("seq") == 0
                ),
            )
            assert telemetry_status["client_telemetry"]["max_rtt_ms"] == 7.5

            def pose(seq: int, grip: bool, x: float = 0.0) -> dict:
                return {
                    "version": 1,
                    "type": "pose",
                    "session_id": session_id,
                    "seq": seq,
                    "client_time_ms": float(seq),
                    "hand": "right",
                    "position_m": [x, 1.0, 0.0],
                    "orientation_xyzw": [0.0, 0.0, 0.0, 1.0],
                    "grip": grip,
                    "trigger": False,
                }

            await ws.send_json(pose(0, False))
            await ws.send_json(pose(1, True, 0.01))
            active = await receive_state(ws, "ACTIVE")
            assert active["tracking"] is True
            assert active["rearm_required"] is False

            await ws.send_json(pose(2, False, 0.01))
            await ws.send_json(pose(3, True, 0.02))
            resumed = await receive_status(
                ws,
                lambda status: (
                    status.get("state") == "ACTIVE"
                    and status.get("counters", {}).get("servo_start_count") == 2
                ),
            )
            assert resumed["tracking"] is True
            assert resumed["rearm_required"] is False
            assert resumed["controller_id"] == session_id
            assert resumed["counters"]["servo_start_count"] == 2
            assert resumed["counters"]["servo_end_count"] == 1

            await ws.send_json(
                {
                    "version": 1,
                    "type": "event",
                    "session_id": session_id,
                    "event": "webxr_ended",
                }
            )
            idle = await receive_state(ws, "IDLE")
            assert idle["tracking"] is False
            await ws.close()
