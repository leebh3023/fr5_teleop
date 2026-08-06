from __future__ import annotations

import time
from dataclasses import dataclass

from teleop.control_math import TcpPose
from teleop.robot.client import RobotClientError


@dataclass(frozen=True)
class FakeRobotBehavior:
    fail_on: str | None = None
    fail_code: int = 99
    hang_on: str | None = None
    hang_seconds: float = 0.0


class FakeRobotClient:
    def __init__(self, behavior: FakeRobotBehavior | None = None) -> None:
        self.behavior = behavior or FakeRobotBehavior()
        self.connected = False
        self.servo_active = False
        self.current_tcp: TcpPose = (300.0, 0.0, 400.0, 180.0, 0.0, 0.0)

    def _before(self, operation: str) -> None:
        if self.behavior.hang_on == operation:
            time.sleep(self.behavior.hang_seconds)
        if self.behavior.fail_on == operation:
            raise RobotClientError(operation, self.behavior.fail_code, "injected failure")

    def connect(self) -> None:
        self._before("connect")
        self.connected = True

    def initialize(self) -> None:
        self._before("initialize")
        if not self.connected:
            raise RobotClientError("initialize", None, "not connected")

    def get_current_tcp(self) -> TcpPose:
        self._before("get_current_tcp")
        return self.current_tcp

    def servo_start(self) -> None:
        self._before("servo_start")
        self.servo_active = True

    def servo_cart(self, target: TcpPose) -> None:
        self._before("servo_cart")
        if not self.servo_active:
            raise RobotClientError("servo_cart", None, "servo is not active")
        self.current_tcp = tuple(target)

    def servo_end(self) -> None:
        self._before("servo_end")
        self.servo_active = False

    def reset_fault(self) -> None:
        self._before("reset_fault")

    def close(self) -> None:
        self._before("close")
        self.connected = False
