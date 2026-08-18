from __future__ import annotations

import math
from dataclasses import dataclass

from teleop.config import TeleopConfig


Vector3 = tuple[float, float, float]
TcpPose = tuple[float, float, float, float, float, float]


def _add(a: Vector3, b: Vector3) -> Vector3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _subtract(a: Vector3, b: Vector3) -> Vector3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale(vector: Vector3, factor: float) -> Vector3:
    return (vector[0] * factor, vector[1] * factor, vector[2] * factor)


def _norm(vector: Vector3) -> float:
    return math.sqrt(sum(value * value for value in vector))


def vr_to_robot_delta(vr_delta_m: Vector3, scale_mm_per_m: float) -> Vector3:
    dx, dy, dz = vr_delta_m
    return (dx * scale_mm_per_m, -dz * scale_mm_per_m, dy * scale_mm_per_m)


def clamp_step(delta: Vector3, max_step_mm: float) -> Vector3:
    distance = _norm(delta)
    if distance <= max_step_mm or distance == 0.0:
        return delta
    return _scale(delta, max_step_mm / distance)


def clamp_workspace(target: TcpPose, config: TeleopConfig) -> TcpPose:
    workspace = config.workspace
    return (
        min(max(target[0], workspace.x[0]), workspace.x[1]),
        min(max(target[1], workspace.y[0]), workspace.y[1]),
        min(max(target[2], workspace.z[0]), workspace.z[1]),
        target[3],
        target[4],
        target[5],
    )


@dataclass
class MotionPlanner:
    config: TeleopConfig
    vr_origin: Vector3 | None = None
    robot_origin: TcpPose | None = None
    filtered_position: Vector3 | None = None
    last_target: TcpPose | None = None

    def engage(self, vr_position: Vector3, robot_tcp: TcpPose) -> None:
        self.vr_origin = tuple(vr_position)
        self.robot_origin = tuple(robot_tcp)
        self.filtered_position = tuple(vr_position)
        self.last_target = tuple(robot_tcp)

    def release(self) -> None:
        self.vr_origin = None
        self.robot_origin = None
        self.filtered_position = None
        self.last_target = None

    def target_for(
        self,
        vr_position: Vector3,
        *,
        step_limit_mm: float | None = None,
    ) -> TcpPose:
        if (
            self.vr_origin is None
            or self.robot_origin is None
            or self.filtered_position is None
            or self.last_target is None
        ):
            raise RuntimeError("motion planner is not engaged")

        limit_mm = (
            self.config.max_step_mm
            if step_limit_mm is None
            else step_limit_mm
        )
        if (
            not math.isfinite(limit_mm)
            or limit_mm < 0
            or limit_mm > self.config.max_step_mm
        ):
            raise ValueError(
                "step_limit_mm must be finite and within max_step_mm"
            )

        alpha = self.config.ema_alpha
        self.filtered_position = (
            alpha * vr_position[0] + (1.0 - alpha) * self.filtered_position[0],
            alpha * vr_position[1] + (1.0 - alpha) * self.filtered_position[1],
            alpha * vr_position[2] + (1.0 - alpha) * self.filtered_position[2],
        )

        vr_delta = _subtract(self.filtered_position, self.vr_origin)
        robot_delta = vr_to_robot_delta(vr_delta, self.config.position_scale)
        desired_position = _add(self.robot_origin[:3], robot_delta)
        previous_position = self.last_target[:3]
        limited_position = _add(
            previous_position,
            clamp_step(
                _subtract(desired_position, previous_position),
                limit_mm,
            ),
        )
        target: TcpPose = (
            limited_position[0],
            limited_position[1],
            limited_position[2],
            self.robot_origin[3],
            self.robot_origin[4],
            self.robot_origin[5],
        )
        target = clamp_workspace(target, self.config)
        self.last_target = target
        return target
