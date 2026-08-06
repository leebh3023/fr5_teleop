from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from teleop.control_math import TcpPose


class WorkerState(str, Enum):
    STARTING = "STARTING"
    IDLE = "IDLE"
    SLEEPING = "SLEEPING"
    ARMING = "ARMING"
    ACTIVE = "ACTIVE"
    STOPPING = "STOPPING"
    FAULT = "FAULT"
    SHUTDOWN = "SHUTDOWN"


class ControlCommand(str, Enum):
    RELEASE = "RELEASE"
    SESSION_LOST = "SESSION_LOST"
    GRIP_PRESSED = "GRIP_PRESSED"
    GRIP_RELEASED = "GRIP_RELEASED"
    FAULT_RESET = "FAULT_RESET"
    SHUTDOWN = "SHUTDOWN"


@dataclass
class WorkerCounters:
    servo_start_count: int = 0
    servo_cart_count: int = 0
    servo_end_count: int = 0
    missed_ticks: int = 0


@dataclass
class WorkerStatus:
    generation: int
    state: WorkerState
    tracking: bool
    rearm_required: bool
    tick: int
    input_age_ms: float | None
    robot_tcp: TcpPose | None
    jitter_ms: float
    sdk_call_ms: float
    counters: WorkerCounters
    fault: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["state"] = self.state.value
        return result


@dataclass
class ServoTransitionMonitor:
    window_ns: int
    limit: int
    _events: deque[int] = field(default_factory=deque)
    _last_report_ns: int | None = None

    def record(self, now_ns: int) -> int | None:
        cutoff_ns = now_ns - self.window_ns
        while self._events and self._events[0] < cutoff_ns:
            self._events.popleft()
        self._events.append(now_ns)

        if len(self._events) < self.limit:
            return None
        if (
            self._last_report_ns is not None
            and now_ns - self._last_report_ns < self.window_ns
        ):
            return None
        self._last_report_ns = now_ns
        return len(self._events)
