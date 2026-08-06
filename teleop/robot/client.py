from __future__ import annotations

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


class RobotClient(Protocol):
    def connect(self) -> None: ...

    def initialize(self) -> None: ...

    def get_current_tcp(self) -> TcpPose: ...

    def servo_start(self) -> None: ...

    def servo_cart(self, target: TcpPose) -> None: ...

    def servo_end(self) -> None: ...

    def reset_fault(self) -> None: ...

    def close(self) -> None: ...
