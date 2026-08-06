from __future__ import annotations

import importlib
import logging
import platform
import sys
from pathlib import Path
from typing import Any

from teleop.config import TeleopConfig
from teleop.control_math import TcpPose
from teleop.robot.client import RobotClientError


log = logging.getLogger(__name__)


class FairinoRobotClient:
    def __init__(self, config: TeleopConfig) -> None:
        if not config.robot_ip:
            raise ValueError("robot_ip is required for FairinoRobotClient")
        self.config = config
        self._robot_module: Any = None
        self._robot: Any = None

    @staticmethod
    def _code(result: Any, operation: str) -> int:
        value = result[0] if isinstance(result, (tuple, list)) and result else result
        if isinstance(value, bool) or not isinstance(value, int):
            raise RobotClientError(
                operation, None, f"unexpected SDK result: {result!r}"
            )
        return value

    @classmethod
    def _expect_zero(cls, result: Any, operation: str) -> None:
        code = cls._code(result, operation)
        if code != 0:
            raise RobotClientError(operation, code)

    def _call_zero(self, operation: str, *args: Any, **kwargs: Any) -> None:
        if self._robot is None:
            raise RobotClientError(operation, None, "robot is not connected")
        method = getattr(self._robot, operation)
        self._expect_zero(method(*args, **kwargs), operation)

    def connect(self) -> None:
        if platform.system() != "Linux":
            raise RobotClientError("connect", None, "Fairino target requires Linux")
        if platform.machine() not in {"x86_64", "AMD64"}:
            raise RobotClientError(
                "connect", None, f"unsupported architecture: {platform.machine()}"
            )

        sdk_path = Path(self.config.sdk_path).resolve()
        robot_source = sdk_path / "fairino" / "Robot.py"
        if not robot_source.is_file():
            raise RobotClientError("connect", None, f"SDK not found: {robot_source}")
        if str(sdk_path) not in sys.path:
            sys.path.insert(0, str(sdk_path))

        self._robot_module = importlib.import_module("fairino.Robot")
        self._robot = self._robot_module.RPC(self.config.robot_ip)
        if not bool(getattr(self._robot_module.RPC, "is_connect", False)):
            raise RobotClientError(
                "connect", -4, "SDK did not establish both CNDE and XML-RPC links"
            )

        version_result = self._robot.GetSDKVersion()
        self._expect_zero(version_result, "GetSDKVersion")
        versions = version_result[1] if isinstance(version_result, tuple) else None
        log.info("Fairino connection ready: ip=%s versions=%s", self.config.robot_ip, versions)

    def initialize(self) -> None:
        self._call_zero("ResetAllError")
        self._call_zero("Mode", 0)
        self._call_zero("DragTeachSwitch", 0)
        self._call_zero("RobotEnable", 1)

    def get_current_tcp(self) -> TcpPose:
        if self._robot is None:
            raise RobotClientError("GetActualTCPPose", None, "robot is not connected")
        result = self._robot.GetActualTCPPose()
        code = self._code(result, "GetActualTCPPose")
        if code != 0:
            raise RobotClientError("GetActualTCPPose", code)
        if (
            not isinstance(result, (tuple, list))
            or len(result) < 2
            or not isinstance(result[1], (tuple, list))
            or len(result[1]) != 6
        ):
            raise RobotClientError(
                "GetActualTCPPose", None, f"unexpected SDK result: {result!r}"
            )
        pose = tuple(float(value) for value in result[1])
        return (pose[0], pose[1], pose[2], pose[3], pose[4], pose[5])

    def servo_start(self) -> None:
        self._call_zero("ServoMoveStart")

    def servo_cart(self, target: TcpPose) -> None:
        if self._robot is None:
            raise RobotClientError("ServoCart", None, "robot is not connected")
        result = self._robot.ServoCart(
            mode=0,
            desc_pos=list(target),
            exaxis=list(self.config.exaxis_default),
            pos_gain=[1.0] * 6,
            acc=100.0,
            vel=100.0,
            cmdT=self.config.servo_period_s,
            filterT=0.05,
            gain=0.0,
        )
        self._expect_zero(result, "ServoCart")

    def servo_end(self) -> None:
        self._call_zero("ServoMoveEnd")

    def reset_fault(self) -> None:
        self._call_zero("ResetAllError")
        self._call_zero("Mode", 0)
        self._call_zero("RobotEnable", 1)

    def close(self) -> None:
        if self._robot is None:
            return
        try:
            self._robot.CloseRPC()
        finally:
            self._robot = None
