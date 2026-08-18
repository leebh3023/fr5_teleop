from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from teleop.config import TeleopConfig
from teleop.controller_lease import ControllerLease
from teleop.protocol import (
    PROTOCOL_VERSION,
    ClientTelemetry,
    ControlEvent,
    PoseMessage,
    ProtocolError,
    parse_client_message,
)
from teleop.robot.state import ControlCommand, WorkerState, WorkerStatus
from teleop.robot.supervisor import RobotSupervisor


log = logging.getLogger(__name__)


@dataclass
class PoseStreamDiagnostics:
    last_seq: int | None = None
    last_received_ns: int | None = None
    last_client_time_ms: float | None = None
    window_started_ns: int | None = None
    sample_count: int = 0
    missing_sequence_count: int = 0
    max_receive_gap_ms: float = 0.0
    max_client_gap_ms: float = 0.0
    last_warning_ns: int = 0
    suppressed_warning_count: int = 0

    def observe(
        self,
        message: PoseMessage,
    ) -> tuple[float | None, float | None, int]:
        receive_gap_ms = None
        client_gap_ms = None
        missing_sequences = 0
        if self.last_received_ns is not None:
            receive_gap_ms = max(
                0.0,
                (message.received_ns - self.last_received_ns) / 1_000_000,
            )
            self.max_receive_gap_ms = max(
                self.max_receive_gap_ms,
                receive_gap_ms,
            )
        if self.last_client_time_ms is not None:
            client_gap_ms = max(
                0.0,
                message.client_time_ms - self.last_client_time_ms,
            )
            self.max_client_gap_ms = max(self.max_client_gap_ms, client_gap_ms)
        if self.last_seq is not None:
            missing_sequences = max(0, message.seq - self.last_seq - 1)
            self.missing_sequence_count += missing_sequences

        if self.window_started_ns is None:
            self.window_started_ns = message.received_ns
        self.sample_count += 1
        self.last_seq = message.seq
        self.last_received_ns = message.received_ns
        self.last_client_time_ms = message.client_time_ms
        return receive_gap_ms, client_gap_ms, missing_sequences

    def take_summary(
        self,
        now_ns: int,
        *,
        force: bool = False,
        interval_ns: int = 10_000_000_000,
    ) -> dict[str, int | float] | None:
        if self.window_started_ns is None or self.sample_count == 0:
            return None
        if not force and now_ns - self.window_started_ns < interval_ns:
            return None
        duration_ms = max(0.0, (now_ns - self.window_started_ns) / 1_000_000)
        summary: dict[str, int | float] = {
            "samples": self.sample_count,
            "missing_sequences": self.missing_sequence_count,
            "max_receive_gap_ms": self.max_receive_gap_ms,
            "max_client_gap_ms": self.max_client_gap_ms,
            "window_ms": duration_ms,
        }
        self.window_started_ns = now_ns
        self.sample_count = 0
        self.missing_sequence_count = 0
        self.max_receive_gap_ms = 0.0
        self.max_client_gap_ms = 0.0
        return summary


class TeleopRuntime:
    def __init__(self, config: TeleopConfig) -> None:
        self.config = config
        self.supervisor = RobotSupervisor(config)
        self.lease = ControllerLease()
        self.clients: dict[str, web.WebSocketResponse] = {}
        self.status_task: asyncio.Task[None] | None = None
        self.supervisor_fault: dict[str, str | None] = {
            h: None for h in self.supervisor.hands
        }
        self._watchdog_handled: dict[str, bool] = {
            h: False for h in self.supervisor.hands
        }
        self._pose_diagnostics: dict[str, PoseStreamDiagnostics] = {}
        self._client_telemetry: dict[str, ClientTelemetry] = {}
        self._telemetry_last_log_ns: dict[str, int] = {}
        self._telemetry_last_warning_ns: dict[str, int] = {}

    async def start(self) -> None:
        self.supervisor.start()
        self.status_task = asyncio.create_task(
            self._status_loop(), name="teleop-status-publisher"
        )

    async def stop(self) -> None:
        self.supervisor.invalidate_pose()
        if self.status_task is not None:
            self.status_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.status_task
        report = await asyncio.to_thread(self.supervisor.shutdown)
        log.info("robot worker shutdown: %s", report)

    async def register(self, session_id: str, ws: web.WebSocketResponse) -> None:
        self.clients[session_id] = ws
        log.info(
            "WebSocket connected session=%s clients=%d",
            session_id[:8],
            len(self.clients),
        )

    async def unregister(self, session_id: str) -> None:
        self.clients.pop(session_id, None)
        diagnostics = self._pose_diagnostics.pop(session_id, None)
        telemetry = self._client_telemetry.pop(session_id, None)
        self._telemetry_last_log_ns.pop(session_id, None)
        self._telemetry_last_warning_ns.pop(session_id, None)
        if diagnostics is not None:
            summary = diagnostics.take_summary(time.monotonic_ns(), force=True)
            if summary is not None:
                log.info(
                    "pose stream final session=%s samples=%d missing_sequences=%d "
                    "max_receive_gap_ms=%.1f max_client_gap_ms=%.1f window_ms=%.1f",
                    session_id[:8],
                    summary["samples"],
                    summary["missing_sequences"],
                    summary["max_receive_gap_ms"],
                    summary["max_client_gap_ms"],
                    summary["window_ms"],
                )
        if telemetry is not None:
            log.info(
                "client telemetry final session=%s telemetry_seq=%d "
                "xr_frames=%d valid_poses=%d sent=%d drops=%d "
                "tracking_losses=%d last_rtt_ms=%s max_rtt_ms=%s",
                session_id[:8],
                telemetry.seq,
                telemetry.xr_frame_count,
                telemetry.valid_pose_count,
                telemetry.pose_send_count,
                telemetry.pose_drop_count,
                telemetry.tracking_loss_count,
                telemetry.last_rtt_ms,
                telemetry.max_rtt_ms,
            )
        if self.lease.release(session_id):
            self.supervisor.invalidate_pose()
            self.supervisor.send_control(ControlCommand.SESSION_LOST)

    def record_pose_diagnostics(self, message: PoseMessage) -> None:
        diagnostics = self._pose_diagnostics.setdefault(
            message.session_id,
            PoseStreamDiagnostics(),
        )
        receive_gap_ms, client_gap_ms, missing_sequences = diagnostics.observe(
            message
        )
        warning_threshold_ms = max(50.0, self.config.pose_timeout_s * 500)
        gap_detected = (
            missing_sequences > 0
            or (
                receive_gap_ms is not None
                and receive_gap_ms >= warning_threshold_ms
            )
            or (
                client_gap_ms is not None
                and client_gap_ms >= warning_threshold_ms
            )
        )
        if gap_detected:
            if (
                diagnostics.last_warning_ns == 0
                or message.received_ns - diagnostics.last_warning_ns
                >= 1_000_000_000
            ):
                log.warning(
                    "pose stream gap session=%s seq=%d missing_sequences=%d "
                    "receive_gap_ms=%s client_gap_ms=%s "
                    "warning_threshold_ms=%.1f suppressed=%d",
                    message.session_id[:8],
                    message.seq,
                    missing_sequences,
                    (
                        f"{receive_gap_ms:.1f}"
                        if receive_gap_ms is not None
                        else None
                    ),
                    (
                        f"{client_gap_ms:.1f}"
                        if client_gap_ms is not None
                        else None
                    ),
                    warning_threshold_ms,
                    diagnostics.suppressed_warning_count,
                )
                diagnostics.last_warning_ns = message.received_ns
                diagnostics.suppressed_warning_count = 0
            else:
                diagnostics.suppressed_warning_count += 1

        summary = diagnostics.take_summary(message.received_ns)
        if summary is not None:
            log.info(
                "pose stream summary session=%s samples=%d missing_sequences=%d "
                "max_receive_gap_ms=%.1f max_client_gap_ms=%.1f window_ms=%.1f",
                message.session_id[:8],
                summary["samples"],
                summary["missing_sequences"],
                summary["max_receive_gap_ms"],
                summary["max_client_gap_ms"],
                summary["window_ms"],
            )

    def record_client_telemetry(self, message: ClientTelemetry) -> None:
        previous = self._client_telemetry.get(message.session_id)
        if previous is not None and message.seq <= previous.seq:
            raise ProtocolError("telemetry sequence must increase")

        pose_drop_delta = max(
            0,
            (
                message.pose_drop_count - previous.pose_drop_count
                if previous is not None
                else message.pose_drop_count
            ),
        )
        tracking_loss_delta = max(
            0,
            (
                message.tracking_loss_count
                - previous.tracking_loss_count
                if previous is not None
                else message.tracking_loss_count
            ),
        )
        self._client_telemetry[message.session_id] = message

        warning_threshold_ms = max(
            50.0,
            self.config.pose_timeout_s * 500,
        )
        degraded = (
            pose_drop_delta > 0
            or tracking_loss_delta > 0
            or message.max_xr_frame_gap_ms >= warning_threshold_ms
            or message.max_pose_gap_ms >= warning_threshold_ms
            or (
                message.max_rtt_ms is not None
                and message.max_rtt_ms >= warning_threshold_ms
            )
            or message.ws_buffered_amount >= 64 * 1024
        )
        suspected_causes: list[str] = []
        if (
            message.max_rtt_ms is not None
            and message.max_rtt_ms >= warning_threshold_ms
        ) or message.ws_buffered_amount >= 64 * 1024 or pose_drop_delta > 0:
            suspected_causes.append("wifi_tcp_or_backpressure")
        if message.max_xr_frame_gap_ms >= warning_threshold_ms:
            suspected_causes.append("quest_frame_or_main_thread")
        if (
            tracking_loss_delta > 0
            or message.max_pose_gap_ms >= warning_threshold_ms
        ):
            suspected_causes.append("controller_tracking")
        suspected = ",".join(suspected_causes) or "none"
        now_ns = message.received_ns
        last_warning_ns = self._telemetry_last_warning_ns.get(
            message.session_id,
            0,
        )
        if degraded and (
            last_warning_ns == 0
            or now_ns - last_warning_ns >= 1_000_000_000
        ):
            log.warning(
                "client telemetry degraded session=%s telemetry_seq=%d "
                "xr_gap_ms=%.1f pose_gap_ms=%.1f last_rtt_ms=%s "
                "max_rtt_ms=%s buffered_bytes=%d pose_drop_delta=%d "
                "tracking_loss_delta=%d suspected=%s",
                message.session_id[:8],
                message.seq,
                message.max_xr_frame_gap_ms,
                message.max_pose_gap_ms,
                message.last_rtt_ms,
                message.max_rtt_ms,
                message.ws_buffered_amount,
                pose_drop_delta,
                tracking_loss_delta,
                suspected,
            )
            self._telemetry_last_warning_ns[message.session_id] = now_ns

        last_log_ns = self._telemetry_last_log_ns.get(message.session_id, 0)
        if last_log_ns == 0 or now_ns - last_log_ns >= 10_000_000_000:
            log.info(
                "client telemetry summary session=%s telemetry_seq=%d "
                "xr_frames=%d valid_poses=%d sent=%d drops=%d "
                "tracking_losses=%d xr_gap_ms=%.1f pose_gap_ms=%.1f "
                "last_rtt_ms=%s max_rtt_ms=%s buffered_bytes=%d",
                message.session_id[:8],
                message.seq,
                message.xr_frame_count,
                message.valid_pose_count,
                message.pose_send_count,
                message.pose_drop_count,
                message.tracking_loss_count,
                message.max_xr_frame_gap_ms,
                message.max_pose_gap_ms,
                message.last_rtt_ms,
                message.max_rtt_ms,
                message.ws_buffered_amount,
            )
            self._telemetry_last_log_ns[message.session_id] = now_ns

    def _controller_telemetry_payload(self) -> dict[str, Any] | None:
        controller_id = self.lease.session_id
        if controller_id is None:
            return None
        telemetry = self._client_telemetry.get(controller_id)
        if telemetry is None:
            return None
        payload = telemetry.to_status_dict()
        payload["age_ms"] = max(
            0.0,
            (time.monotonic_ns() - telemetry.received_ns) / 1_000_000,
        )
        return payload

    def status_payload(self, status: WorkerStatus | None = None) -> dict[str, Any]:
        current = status or self.supervisor.latest_status
        health_map = self.supervisor.health()
        # Combine faults from all arms
        any_fault = any(f for f in self.supervisor_fault.values())
        combined_fault = "; ".join(
            f"{h}: {f}" for h, f in self.supervisor_fault.items() if f
        ) or None
        if current is None:
            first_health = next(iter(health_map.values()), None)
            return {
                "version": PROTOCOL_VERSION,
                "type": "status",
                "state": "STARTING",
                "tracking": False,
                "rearm_required": True,
                "gripper_enabled": self.config.gripper.enabled,
                "gripper_busy": False,
                "gripper_position": (
                    self.config.gripper.closed_position
                    if self.config.gripper.initially_closed
                    else self.config.gripper.open_position
                ),
                "controller_id": self.lease.session_id,
                "client_telemetry": self._controller_telemetry_payload(),
                "ack_seq": self.lease.last_seq if self.lease.last_seq >= 0 else None,
                "worker": {
                    "generation": first_health.generation if first_health else 0,
                    "alive": first_health.alive if first_health else False,
                    "heartbeat_age_ms": first_health.heartbeat_age_ms if first_health else None,
                    "hung": first_health.hung if first_health else False,
                },
                "arms": {
                    h: {
                        "generation": hv.generation,
                        "alive": hv.alive,
                        "heartbeat_age_ms": hv.heartbeat_age_ms,
                        "hung": hv.hung,
                        "fault": self.supervisor_fault.get(h),
                    }
                    for h, hv in health_map.items()
                },
                "fault": combined_fault,
            }
        payload = current.to_dict()
        first_health = next(iter(health_map.values()), None)
        payload.update(
            {
                "version": PROTOCOL_VERSION,
                "type": "status",
                "controller_id": self.lease.session_id,
                "client_telemetry": self._controller_telemetry_payload(),
                "ack_seq": self.lease.last_seq if self.lease.last_seq >= 0 else None,
                "worker": {
                    "generation": first_health.generation if first_health else 0,
                    "alive": first_health.alive if first_health else False,
                    "heartbeat_age_ms": first_health.heartbeat_age_ms if first_health else None,
                    "hung": first_health.hung if first_health else False,
                },
                "arms": {
                    h: {
                        "generation": hv.generation,
                        "alive": hv.alive,
                        "heartbeat_age_ms": hv.heartbeat_age_ms,
                        "hung": hv.hung,
                        "fault": self.supervisor_fault.get(h),
                    }
                    for h, hv in health_map.items()
                },
            }
        )
        if any_fault:
            payload["state"] = WorkerState.FAULT.value
            payload["tracking"] = False
            payload["fault"] = combined_fault
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
            all_statuses = self.supervisor.drain_status()
            # Find any latest status to broadcast
            last_status: WorkerStatus | None = None
            for hand_statuses in all_statuses.values():
                if hand_statuses:
                    last_status = hand_statuses[-1]
            await self.broadcast(self.status_payload(last_status))

            health_map = self.supervisor.health()
            for hand, hv in health_map.items():
                if hv.hung and not self._watchdog_handled.get(hand, False):
                    self._watchdog_handled[hand] = True
                    self.supervisor_fault[hand] = "robot_worker_watchdog_timeout"
                    self.supervisor.invalidate_pose(hand=hand)
                    log.error(
                        "robot worker heartbeat timed out hand=%s after %.1f ms",
                        hand,
                        hv.heartbeat_age_ms or -1,
                    )
                    # Only release lease if all arms are faulted or this is the only arm
                    all_faulted = all(
                        self._watchdog_handled.get(h, False) for h in health_map
                    )
                    if all_faulted:
                        owner = self.lease.session_id
                        if owner:
                            self.lease.release(owner)
                    report = await asyncio.to_thread(
                        self.supervisor.shutdown
                    )
                    log.error("hung robot worker contained: %s", report)
                    await self.broadcast(self.status_payload())
                elif not hv.alive and not self._watchdog_handled.get(hand, False):
                    self._watchdog_handled[hand] = True
                    self.supervisor_fault[hand] = "robot_worker_exited"
                    all_faulted = all(
                        self._watchdog_handled.get(h, False) for h in health_map
                    )
                    if all_faulted:
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


async def monitor_handler(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(
        _runtime(request).config.web_dir / "monitor.html"
    )


async def live_handler(request: web.Request) -> web.Response:
    return web.json_response({"live": True})


async def ready_handler(request: web.Request) -> web.Response:
    runtime = _runtime(request)
    health_map = runtime.supervisor.health()
    status = runtime.supervisor.latest_status
    all_alive = all(h.alive and not h.hung for h in health_map.values())
    no_fault = not any(f for f in runtime.supervisor_fault.values())
    ready = (
        all_alive
        and no_fault
        and status is not None
        and status.state
        in {
            WorkerState.IDLE,
            WorkerState.SLEEPING,
            WorkerState.ARMING,
            WorkerState.ACTIVE,
            WorkerState.GRIPPER_ACTION,
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

    last_protocol_warning_ns = 0
    suppressed_protocol_warnings = 0
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
                now_ns = time.monotonic_ns()
                if (
                    last_protocol_warning_ns == 0
                    or now_ns - last_protocol_warning_ns >= 1_000_000_000
                ):
                    log.warning(
                        "WebSocket protocol error session=%s error=%s suppressed=%d",
                        session_id[:8],
                        exc,
                        suppressed_protocol_warnings,
                    )
                    last_protocol_warning_ns = now_ns
                    suppressed_protocol_warnings = 0
                else:
                    suppressed_protocol_warnings += 1
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
    message: PoseMessage | ControlEvent | ClientTelemetry,
) -> None:
    if isinstance(message, ClientTelemetry):
        if not runtime.lease.owns(message.session_id):
            raise ProtocolError(
                "only the controller may publish client telemetry"
            )
        runtime.record_client_telemetry(message)
        await ws.send_json(
            {
                "version": PROTOCOL_VERSION,
                "type": "telemetry_ack",
                "seq": message.seq,
                "client_time_ms": message.client_time_ms,
            }
        )
        return

    if isinstance(message, ControlEvent):
        if message.event in {"claim_control", "webxr_started"}:
            granted = runtime.lease.claim(message.session_id)
            log.info(
                "controller claim session=%s event=%s granted=%s",
                message.session_id[:8],
                message.event,
                granted,
            )
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
            released = runtime.lease.release(message.session_id)
            log.info(
                "controller release session=%s event=%s released=%s",
                message.session_id[:8],
                message.event,
                released,
            )
            if released:
                runtime.supervisor.invalidate_pose()
                runtime.supervisor.send_control(ControlCommand.RELEASE)
            return
        if message.event == "fault_reset":
            if not runtime.lease.owns(message.session_id):
                raise ProtocolError("only the controller may reset a fault")
            log.warning(
                "controller fault reset requested session=%s",
                message.session_id[:8],
            )
            runtime.supervisor.send_control(ControlCommand.FAULT_RESET)
            return

    if not runtime.lease.owns(message.session_id):
        raise ProtocolError("this session does not own the controller lease")
    if not runtime.lease.accept_sequence(message.session_id, message.seq, message.hand):
        raise ProtocolError("pose sequence must increase")
    runtime.record_pose_diagnostics(message)
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
    app.router.add_get("/monitor", monitor_handler)
    app.router.add_get("/health/live", live_handler)
    app.router.add_get("/health/ready", ready_handler)
    app.router.add_get("/ws", websocket_handler)
    static_dir = Path(config.web_dir)
    app.router.add_static("/static", static_dir, show_index=False)
    return app
