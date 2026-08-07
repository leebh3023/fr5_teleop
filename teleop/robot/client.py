from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from teleop.control_math import TcpPose


class RobotClientError(RuntimeError):
    def __init__(self, operation: str, code: int | None, detail: str = "") -> None:
        self.operation = operation
        self.code = code
        self.detail = detail
        message = f"{operation} failed"
        if code is not None:
            message += f" with code {code}"
        if detail:
            message += f": {detail}"
        super().__init__(message)


@dataclass(frozen=True)
class GripperMotionState:
    fault: int
    done: bool


class RobotClient(Protocol):
    def connect(self) -> None: ...

    def initialize(self) -> None: ...

    def get_current_tcp(self) -> TcpPose: ...

    def servo_start(self) -> None: ...

    def servo_cart(self, target: TcpPose) -> None: ...

    def servo_end(self) -> None: ...

    def activate_gripper(self) -> None: ...

    def move_gripper(self, position: int) -> None: ...

    def get_gripper_motion_state(self) -> GripperMotionState: ...

    def reset_fault(self) -> None: ...

    def close(self) -> None: ...
