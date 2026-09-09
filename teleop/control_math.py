from __future__ import annotations

import math
from dataclasses import dataclass

from teleop.config import TeleopConfig


Vector3 = tuple[float, float, float]
TcpPose = tuple[float, float, float, float, float, float]
Quaternion = tuple[float, float, float, float]  # (x, y, z, w), matches protocol.orientation_xyzw


def _add(a: Vector3, b: Vector3) -> Vector3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _subtract(a: Vector3, b: Vector3) -> Vector3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale(vector: Vector3, factor: float) -> Vector3:
    return (vector[0] * factor, vector[1] * factor, vector[2] * factor)


def _norm(vector: Vector3) -> float:
    return math.sqrt(sum(value * value for value in vector))


def remap_axis_vr_to_robot(vector: Vector3) -> Vector3:
    x, y, z = vector
    return (x, z, y)


def vr_to_robot_delta(vr_delta_m: Vector3, scale_mm_per_m: float) -> Vector3:
    return _scale(remap_axis_vr_to_robot(vr_delta_m), scale_mm_per_m)


# --- Quaternion helpers -----------------------------------------------
#
# rx, ry, rz on TcpPose are Fairino's Cartesian Euler angles in degrees.
# The exact axis order/convention is not documented anywhere in the
# vendored Python SDK. euler_deg_to_quat/quat_to_euler_deg below assume
# the common industrial-arm "fixed-angle XYZ" convention (R = Rz * Ry *
# Rx). This assumption is isolated to those two functions plus
# remap_quat_vr_to_robot/compose_delta_orientation, so a wrong guess is a
# targeted fix after real-robot single-axis verification (see AGENTS.md),
# not a broader redesign.


def quat_normalize(q: Quaternion) -> Quaternion:
    x, y, z, w = q
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm < 1e-9:
        return (0.0, 0.0, 0.0, 1.0)
    return (x / norm, y / norm, z / norm, w / norm)


def quat_conjugate(q: Quaternion) -> Quaternion:
    x, y, z, w = q
    return (-x, -y, -z, w)


def quat_multiply(a: Quaternion, b: Quaternion) -> Quaternion:
    """Hamilton product a*b: rotating by b then by a."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quat_dot(a: Quaternion, b: Quaternion) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]


def quat_delta(origin: Quaternion, current: Quaternion) -> Quaternion:
    """Rotation q such that current == quat_multiply(q, origin)."""
    return quat_normalize(quat_multiply(current, quat_conjugate(origin)))


def quat_axis_angle(q: Quaternion) -> tuple[Vector3, float]:
    x, y, z, w = quat_normalize(q)
    if w < 0.0:
        x, y, z, w = -x, -y, -z, -w
    w = min(1.0, max(-1.0, w))
    angle = 2.0 * math.acos(w)
    s = math.sqrt(max(0.0, 1.0 - w * w))
    if s < 1e-9:
        return (0.0, 0.0, 1.0), 0.0
    return (x / s, y / s, z / s), angle


def axis_angle_to_quat(axis: Vector3, angle_rad: float) -> Quaternion:
    norm = _norm(axis)
    if norm < 1e-9:
        return (0.0, 0.0, 0.0, 1.0)
    ax, ay, az = axis[0] / norm, axis[1] / norm, axis[2] / norm
    half = angle_rad / 2.0
    s = math.sin(half)
    return (ax * s, ay * s, az * s, math.cos(half))


def quat_slerp(a: Quaternion, b: Quaternion, t: float) -> Quaternion:
    a = quat_normalize(a)
    b = quat_normalize(b)
    dot = quat_dot(a, b)
    if dot < 0.0:
        b = (-b[0], -b[1], -b[2], -b[3])
        dot = -dot
    dot = min(1.0, max(-1.0, dot))
    if dot > 0.9995:
        result = tuple(a[i] + t * (b[i] - a[i]) for i in range(4))
        return quat_normalize(result)  # type: ignore[return-value]
    theta_0 = math.acos(dot)
    theta = theta_0 * t
    sin_theta_0 = math.sin(theta_0)
    sin_theta = math.sin(theta)
    s0 = math.cos(theta) - dot * sin_theta / sin_theta_0
    s1 = sin_theta / sin_theta_0
    result = tuple(s0 * a[i] + s1 * b[i] for i in range(4))
    return quat_normalize(result)  # type: ignore[return-value]


def remap_quat_vr_to_robot(q: Quaternion) -> Quaternion:
    axis, angle = quat_axis_angle(q)
    return axis_angle_to_quat(remap_axis_vr_to_robot(axis), angle)


def euler_deg_to_quat(rx_deg: float, ry_deg: float, rz_deg: float) -> Quaternion:
    qx = axis_angle_to_quat((1.0, 0.0, 0.0), math.radians(rx_deg))
    qy = axis_angle_to_quat((0.0, 1.0, 0.0), math.radians(ry_deg))
    qz = axis_angle_to_quat((0.0, 0.0, 1.0), math.radians(rz_deg))
    return quat_normalize(quat_multiply(quat_multiply(qz, qy), qx))


def quat_to_euler_deg(q: Quaternion) -> Vector3:
    x, y, z, w = quat_normalize(q)

    sinp = 2.0 * (w * y - z * x)
    if abs(sinp) >= 1.0 - 1e-9:
        # Gimbal lock (ry = +-90 deg): only roll-yaw (north pole) or
        # roll+yaw (south pole) is determined. Roll is arbitrarily pinned
        # to 0 and the coupled angle is folded entirely into yaw, so the
        # rebuilt quaternion still reproduces the original rotation even
        # though the individual roll/yaw split is not unique here.
        pitch = math.copysign(math.pi / 2.0, sinp)
        roll = 0.0
        yaw = -2.0 * math.atan2(x, w) if sinp > 0 else 2.0 * math.atan2(x, w)
        return (math.degrees(roll), math.degrees(pitch), math.degrees(yaw))

    pitch = math.asin(sinp)

    sinr_cosp = 2.0 * (w * x + y * z)
    cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(siny_cosp, cosy_cosp)

    return (math.degrees(roll), math.degrees(pitch), math.degrees(yaw))


def compose_delta_orientation(origin_q: Quaternion, delta_q: Quaternion) -> Quaternion:
    """World-frame composition: delta applied on top of origin.

    If real-robot verification (AGENTS.md manual single-axis test)
    shows the SDK actually composes in the tool/local frame instead,
    this is the only function to change (to
    quat_multiply(origin_q, delta_q)).
    """
    return quat_normalize(quat_multiply(delta_q, origin_q))


def clamp_angular_step(
    current: Quaternion, desired: Quaternion, max_step_deg: float
) -> Quaternion:
    delta = quat_delta(current, desired)
    axis, angle_rad = quat_axis_angle(delta)
    angle_deg = math.degrees(angle_rad)
    if angle_deg <= max_step_deg or angle_deg == 0.0:
        return desired
    limited_delta = axis_angle_to_quat(axis, math.radians(max_step_deg))
    return compose_delta_orientation(current, limited_delta)


def clamp_orientation_deviation(
    origin: Quaternion, desired: Quaternion, max_deviation_deg: float
) -> Quaternion:
    delta = quat_delta(origin, desired)
    axis, angle_rad = quat_axis_angle(delta)
    angle_deg = math.degrees(angle_rad)
    if angle_deg <= max_deviation_deg or angle_deg == 0.0:
        return desired
    limited_delta = axis_angle_to_quat(axis, math.radians(max_deviation_deg))
    return compose_delta_orientation(origin, limited_delta)


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
    vr_origin_orientation: Quaternion | None = None
    robot_origin_orientation: Quaternion | None = None
    filtered_orientation: Quaternion | None = None
    last_target_orientation: Quaternion | None = None

    def engage(
        self, vr_position: Vector3, vr_orientation: Quaternion, robot_tcp: TcpPose
    ) -> None:
        self.vr_origin = tuple(vr_position)
        self.robot_origin = tuple(robot_tcp)
        self.filtered_position = tuple(vr_position)
        self.last_target = tuple(robot_tcp)

        origin_orientation = quat_normalize(vr_orientation)
        robot_orientation = euler_deg_to_quat(robot_tcp[3], robot_tcp[4], robot_tcp[5])
        self.vr_origin_orientation = origin_orientation
        self.robot_origin_orientation = robot_orientation
        self.filtered_orientation = origin_orientation
        self.last_target_orientation = robot_orientation

    def release(self) -> None:
        self.vr_origin = None
        self.robot_origin = None
        self.filtered_position = None
        self.last_target = None
        self.vr_origin_orientation = None
        self.robot_origin_orientation = None
        self.filtered_orientation = None
        self.last_target_orientation = None

    def target_for(
        self,
        vr_position: Vector3,
        vr_orientation: Quaternion,
        *,
        step_limit_mm: float | None = None,
        angular_step_limit_deg: float | None = None,
    ) -> TcpPose:
        if (
            self.vr_origin is None
            or self.robot_origin is None
            or self.filtered_position is None
            or self.last_target is None
            or self.vr_origin_orientation is None
            or self.robot_origin_orientation is None
            or self.filtered_orientation is None
            or self.last_target_orientation is None
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

        if self.config.orientation.enabled:
            orientation_config = self.config.orientation
            angular_limit_deg = (
                orientation_config.max_step_deg
                if angular_step_limit_deg is None
                else angular_step_limit_deg
            )
            if (
                not math.isfinite(angular_limit_deg)
                or angular_limit_deg < 0
                or angular_limit_deg > orientation_config.max_step_deg
            ):
                raise ValueError(
                    "angular_step_limit_deg must be finite and within"
                    " orientation.max_step_deg"
                )

            self.filtered_orientation = quat_slerp(
                self.filtered_orientation,
                quat_normalize(vr_orientation),
                orientation_config.ema_alpha,
            )
            vr_delta_q = quat_delta(
                self.vr_origin_orientation, self.filtered_orientation
            )
            robot_delta_q = remap_quat_vr_to_robot(vr_delta_q)
            if orientation_config.scale != 1.0:
                axis, angle_rad = quat_axis_angle(robot_delta_q)
                robot_delta_q = axis_angle_to_quat(
                    axis, angle_rad * orientation_config.scale
                )
            desired_orientation = compose_delta_orientation(
                self.robot_origin_orientation, robot_delta_q
            )
            desired_orientation = clamp_angular_step(
                self.last_target_orientation, desired_orientation, angular_limit_deg
            )
            desired_orientation = clamp_orientation_deviation(
                self.robot_origin_orientation,
                desired_orientation,
                orientation_config.max_deviation_deg,
            )
            self.last_target_orientation = desired_orientation
            rx, ry, rz = quat_to_euler_deg(desired_orientation)
        else:
            rx, ry, rz = self.robot_origin[3], self.robot_origin[4], self.robot_origin[5]

        target: TcpPose = (
            limited_position[0],
            limited_position[1],
            limited_position[2],
            rx,
            ry,
            rz,
        )
        target = clamp_workspace(target, self.config)
        self.last_target = target
        return target
