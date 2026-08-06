from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from teleop.control_math import TcpPose


class WorkerState(str, Enum):
    STARTING = "STARTING"
    IDLE = "IDLE"
    ARMING = "ARMING"
    ACTIVE = "ACTIVE"
    STOPPING = "STOPPING"
    FAULT = "FAULT"
    SHUTDOWN = "SHUTDOWN"


class ControlCommand(str, Enum):
    RELEASE = "RELEASE"
    SESSION_LOST = "SESSION_LOST"
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
