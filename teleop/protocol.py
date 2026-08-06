from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping


PROTOCOL_VERSION = 1
MAX_POSITION_METERS = 10.0


class ProtocolError(ValueError):
    """A client message violates the teleop protocol."""


@dataclass(frozen=True)
class PoseMessage:
    session_id: str
    seq: int
    client_time_ms: float
    hand: str
    position_m: tuple[float, float, float]
    orientation_xyzw: tuple[float, float, float, float]
    grip: bool
    trigger: bool
    received_ns: int


@dataclass(frozen=True)
class ControlEvent:
    session_id: str
    event: str


ClientMessage = PoseMessage | ControlEvent


def _finite_vector(value: Any, length: int, name: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ProtocolError(f"{name} must contain {length} numbers")
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ProtocolError(f"{name} must contain only numbers")
        number = float(item)
        if not math.isfinite(number):
            raise ProtocolError(f"{name} contains a non-finite number")
        result.append(number)
    return tuple(result)


def parse_client_message(payload: Mapping[str, Any], received_ns: int) -> ClientMessage:
    version = payload.get("version")
    if isinstance(version, bool) or version != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version: {payload.get('version')!r}")

    message_type = payload.get("type")
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
        raise ProtocolError("session_id is required")

    if message_type == "event":
        event = payload.get("event")
        allowed = {
            "webxr_started",
            "webxr_ended",
            "claim_control",
            "release_control",
            "fault_reset",
        }
        if event not in allowed:
            raise ProtocolError(f"unsupported event: {event!r}")
        return ControlEvent(session_id=session_id, event=event)

    if message_type != "pose":
        raise ProtocolError(f"unsupported message type: {message_type!r}")

    seq = payload.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ProtocolError("seq must be a non-negative integer")
    client_time_ms = payload.get("client_time_ms")
    if isinstance(client_time_ms, bool) or not isinstance(client_time_ms, (int, float)):
        raise ProtocolError("client_time_ms must be a number")
    client_time_ms = float(client_time_ms)
    if not math.isfinite(client_time_ms) or client_time_ms < 0:
        raise ProtocolError("client_time_ms must be finite and non-negative")

    hand = payload.get("hand")
    if hand not in {"left", "right", "none"}:
        raise ProtocolError("hand must be left, right, or none")

    position = _finite_vector(payload.get("position_m"), 3, "position_m")
    if any(abs(value) > MAX_POSITION_METERS for value in position):
        raise ProtocolError("position_m exceeds the accepted input range")

    orientation = _finite_vector(
        payload.get("orientation_xyzw"), 4, "orientation_xyzw"
    )
    norm = math.sqrt(sum(value * value for value in orientation))
    if norm < 1e-9:
        raise ProtocolError("orientation quaternion has zero length")
    normalized = tuple(value / norm for value in orientation)

    grip = payload.get("grip")
    trigger = payload.get("trigger")
    if not isinstance(grip, bool) or not isinstance(trigger, bool):
        raise ProtocolError("grip and trigger must be booleans")

    return PoseMessage(
        session_id=session_id,
        seq=seq,
        client_time_ms=client_time_ms,
        hand=hand,
        position_m=(position[0], position[1], position[2]),
        orientation_xyzw=(
            normalized[0],
            normalized[1],
            normalized[2],
            normalized[3],
        ),
        grip=grip,
        trigger=trigger,
        received_ns=received_ns,
    )
