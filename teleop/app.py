from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from teleop.config import TeleopConfig
from teleop.controller_lease import ControllerLease
from teleop.protocol import (
    PROTOCOL_VERSION,
    ControlEvent,
    PoseMessage,
    ProtocolError,
    parse_client_message,
)
from teleop.robot.state import ControlCommand, WorkerState, WorkerStatus
from teleop.robot.supervisor import RobotSupervisor


log = logging.getLogger(__name__)


class TeleopRuntime:
    def __init__(self, config: TeleopConfig) -> None:
        self.config = config
        self.supervisor = RobotSupervisor(config)
        self.lease = ControllerLease()
        self.clients: dict[str, web.WebSocketResponse] = {}
        self.status_task: asyncio.Task[None] | None = None
        self.supervisor_fault: str | None = None
        self._watchdog_handled = False

    async def start(self) -> None:
        self.supervisor.start()
        self.status_task = asyncio.create_task(
            self._status_loop(), name="teleop-status-publisher"
        )

    async def stop(self) -> None:
        self.supervisor.invalidate_pose()
        self.supervisor.send_control(ControlCommand.SHUTDOWN)
        if self.status_task is not None:
            self.status_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.status_task
        report = await asyncio.to_thread(self.supervisor.shutdown)
        log.info("robot worker shutdown: %s", report)

    async def register(self, session_id: str, ws: web.WebSocketResponse) -> None:
        self.clients[session_id] = ws

    async def unregister(self, session_id: str) -> None:
        self.clients.pop(session_id, None)
        if self.lease.release(session_id):
            self.supervisor.invalidate_pose()
            self.supervisor.send_control(ControlCommand.SESSION_LOST)

    def status_payload(self, status: WorkerStatus | None = None) -> dict[str, Any]:
        current = status or self.supervisor.latest_status
        health = self.supervisor.health()
        if current is None:
            return {
                "version": PROTOCOL_VERSION,
                "type": "status",
                "state": "STARTING",
                "tracking": False,
                "controller_id": self.lease.session_id,
                "ack_seq": self.lease.last_seq if self.lease.last_seq >= 0 else None,
                "worker": {
                    "generation": health.generation,
                    "alive": health.alive,
                    "heartbeat_age_ms": health.heartbeat_age_ms,
                    "hung": health.hung,
                },
                "fault": self.supervisor_fault,
            }
        payload = current.to_dict()
        payload.update(
            {
                "version": PROTOCOL_VERSION,
                "type": "status",
                "controller_id": self.lease.session_id,
                "ack_seq": self.lease.last_seq if self.lease.last_seq >= 0 else None,
                "worker": {
                    "generation": health.generation,
                    "alive": health.alive,
                    "heartbeat_age_ms": health.heartbeat_age_ms,
                    "hung": health.hung,
                },
            }
        )
        if self.supervisor_fault:
            payload["state"] = WorkerState.FAULT.value
            payload["tracking"] = False
            payload["fault"] = self.supervisor_fault
        return payload

    async def broadcast(self, payload: dict[str, Any]) -> None:
        stale: list[str] = []
        for session_id, ws in tuple(self.clients.items()):
            if ws.closed:
                stale.append(session_id)
                continue
            try:
                await ws.send_json(payload)
            except (ConnectionError, RuntimeError):
                stale.append(session_id)
        for session_id in stale:
            await self.unregister(session_id)

    async def _status_loop(self) -> None:
        interval = 1.0 / self.config.status_hz
        while True:
            statuses = self.supervisor.drain_status()
            if statuses:
                await self.broadcast(self.status_payload(statuses[-1]))
            else:
                await self.broadcast(self.status_payload())

            health = self.supervisor.health()
            if health.hung and not self._watchdog_handled:
                self._watchdog_handled = True
                self.supervisor_fault = "robot_worker_watchdog_timeout"
                self.supervisor.invalidate_pose()
                owner = self.lease.session_id
                if owner:
                    self.lease.release(owner)
                log.error(
                    "robot worker heartbeat timed out after %.1f ms",
                    health.heartbeat_age_ms or -1,
                )
                report = await asyncio.to_thread(self.supervisor.shutdown)
                log.error("hung robot worker contained: %s", report)
                await self.broadcast(self.status_payload())
            elif not health.alive and not self._watchdog_handled:
                self._watchdog_handled = True
                self.supervisor_fault = "robot_worker_exited"
                owner = self.lease.session_id
                if owner:
                    self.lease.release(owner)
                await self.broadcast(self.status_payload())

            await asyncio.sleep(interval)


RUNTIME_KEY = web.AppKey("teleop_runtime", TeleopRuntime)


def _runtime(request: web.Request) -> TeleopRuntime:
    return request.app[RUNTIME_KEY]


async def index_handler(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(_runtime(request).config.web_dir / "index.html")


async def live_handler(request: web.Request) -> web.Response:
    return web.json_response({"live": True})


async def ready_handler(request: web.Request) -> web.Response:
    runtime = _runtime(request)
    health = runtime.supervisor.health()
    status = runtime.supervisor.latest_status
    ready = (
        health.alive
        and not health.hung
        and runtime.supervisor_fault is None
        and status is not None
        and status.state
        in {
            WorkerState.IDLE,
            WorkerState.ARMING,
            WorkerState.ACTIVE,
        }
    )
    return web.json_response(
        {"ready": ready, "status": runtime.status_payload(status)},
        status=200 if ready else 503,
    )


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    runtime = _runtime(request)
    origin = request.headers.get("Origin")
    if (
        runtime.config.allowed_origins
        and origin not in runtime.config.allowed_origins
    ):
        raise web.HTTPForbidden(text="origin is not allowed")

    ws = web.WebSocketResponse(
        max_msg_size=runtime.config.max_ws_message_bytes,
        heartbeat=20.0,
        autoping=True,
    )
    await ws.prepare(request)
    session_id = uuid.uuid4().hex
    await runtime.register(session_id, ws)
    await ws.send_json(
        {
            "version": PROTOCOL_VERSION,
            "type": "hello",
            "session_id": session_id,
            "controller_available": runtime.lease.session_id is None,
        }
    )
    await ws.send_json(runtime.status_payload())

    try:
        async for message in ws:
            if message.type == WSMsgType.ERROR:
                log.warning("WebSocket error session=%s: %s", session_id[:8], ws.exception())
                break
            if message.type != WSMsgType.TEXT:
                continue
            try:
                raw = json.loads(message.data)
                if not isinstance(raw, dict):
                    raise ProtocolError("message must be a JSON object")
                parsed = parse_client_message(raw, time.monotonic_ns())
                if parsed.session_id != session_id:
                    raise ProtocolError("session_id does not match this connection")
                await _handle_client_message(runtime, ws, parsed)
            except (json.JSONDecodeError, ProtocolError) as exc:
                await ws.send_json(
                    {
                        "version": PROTOCOL_VERSION,
                        "type": "error",
                        "code": "protocol_error",
                        "message": str(exc),
                    }
                )
    finally:
        await runtime.unregister(session_id)
        log.info("WebSocket closed session=%s", session_id[:8])
    return ws


async def _handle_client_message(
    runtime: TeleopRuntime,
    ws: web.WebSocketResponse,
    message: PoseMessage | ControlEvent,
) -> None:
    if isinstance(message, ControlEvent):
        if message.event in {"claim_control", "webxr_started"}:
            granted = runtime.lease.claim(message.session_id)
            await ws.send_json(
                {
                    "version": PROTOCOL_VERSION,
                    "type": "control",
                    "granted": granted,
                    "session_id": message.session_id,
                }
            )
            return
        if message.event in {"release_control", "webxr_ended"}:
            if runtime.lease.release(message.session_id):
                runtime.supervisor.invalidate_pose()
                runtime.supervisor.send_control(ControlCommand.RELEASE)
            return
        if message.event == "fault_reset":
            if not runtime.lease.owns(message.session_id):
                raise ProtocolError("only the controller may reset a fault")
            runtime.supervisor.send_control(ControlCommand.FAULT_RESET)
            return

    if not runtime.lease.owns(message.session_id):
        raise ProtocolError("this session does not own the controller lease")
    if not runtime.lease.accept_sequence(message.session_id, message.seq):
        raise ProtocolError("pose sequence must increase")
    runtime.supervisor.publish_pose(message)


async def _application_context(app: web.Application):
    runtime: TeleopRuntime = app[RUNTIME_KEY]
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()


def create_app(config: TeleopConfig) -> web.Application:
    config.validate()
    app = web.Application(client_max_size=config.max_ws_message_bytes)
    app[RUNTIME_KEY] = TeleopRuntime(config)
    app.cleanup_ctx.append(_application_context)
    app.router.add_get("/", index_handler)
    app.router.add_get("/health/live", live_handler)
    app.router.add_get("/health/ready", ready_handler)
    app.router.add_get("/ws", websocket_handler)
    static_dir = Path(config.web_dir)
    app.router.add_static("/static", static_dir, show_index=False)
    return app
