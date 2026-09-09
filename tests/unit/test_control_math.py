import math
from dataclasses import replace

import pytest

from teleop.config import OrientationConfig, TeleopConfig, WorkspaceBounds
from teleop.control_math import (
    MotionPlanner,
    Quaternion,
    axis_angle_to_quat,
    clamp_angular_step,
    clamp_orientation_deviation,
    clamp_step,
    compose_delta_orientation,
    euler_deg_to_quat,
    quat_axis_angle,
    quat_conjugate,
    quat_delta,
    quat_dot,
    quat_multiply,
    quat_normalize,
    quat_slerp,
    quat_to_euler_deg,
    remap_axis_vr_to_robot,
    remap_quat_vr_to_robot,
    vr_to_robot_delta,
)


IDENTITY_Q: Quaternion = (0.0, 0.0, 0.0, 1.0)


def _web_config(tmp_path, **overrides) -> TeleopConfig:
    web_dir = tmp_path / "web"
    web_dir.mkdir(exist_ok=True)
    web_dir.joinpath("index.html").write_text("", encoding="utf-8")
    web_dir.joinpath("monitor.html").write_text("", encoding="utf-8")
    return TeleopConfig(
        web_dir=web_dir,
        tls_cert_path=None,
        tls_key_path=None,
        **overrides,
    )


def test_vr_axis_mapping() -> None:
    assert vr_to_robot_delta((1.0, 2.0, 3.0), 500.0) == (
        500.0,
        1500.0,
        1000.0,
    )


def test_remap_axis_vr_to_robot_matches_position_mapping() -> None:
    assert remap_axis_vr_to_robot((1.0, 2.0, 3.0)) == (1.0, 3.0, 2.0)


def test_step_is_limited_by_vector_length() -> None:
    assert clamp_step((3.0, 4.0, 0.0), 2.5) == pytest.approx((1.5, 2.0, 0.0))


def test_engage_resets_previous_target_and_clamps_workspace(tmp_path) -> None:
    config = _web_config(
        tmp_path,
        ema_alpha=1.0,
        max_step_mm=1000.0,
        workspace=WorkspaceBounds(x=(-10, 310), y=(-20, 20), z=(50, 450)),
    )
    planner = MotionPlanner(config)
    origin = (300.0, 0.0, 400.0, 180.0, 0.0, 0.0)
    planner.engage((0.0, 0.0, 0.0), IDENTITY_Q, origin)
    assert planner.target_for((0.0, 1.0, -1.0), IDENTITY_Q) == (
        300,
        -20,
        450,
        180,
        0,
        0,
    )
    planner.engage((5.0, 5.0, 5.0), IDENTITY_Q, origin)
    assert planner.last_target == origin


def test_target_accepts_time_based_step_below_absolute_cap(tmp_path) -> None:
    config = _web_config(
        tmp_path,
        ema_alpha=1.0,
        max_velocity_mm_s=50.0,
        max_step_mm=1.0,
    )
    planner = MotionPlanner(config)
    origin = (300.0, 0.0, 400.0, 180.0, 0.0, 0.0)
    planner.engage((0.0, 0.0, 0.0), IDENTITY_Q, origin)

    target = planner.target_for(
        (1.0, 0.0, 0.0),
        IDENTITY_Q,
        step_limit_mm=0.4,
    )

    assert target[:3] == pytest.approx((300.4, 0.0, 400.0))


def test_target_rejects_step_above_absolute_cap(tmp_path) -> None:
    config = _web_config(tmp_path, max_velocity_mm_s=50.0, max_step_mm=1.0)
    planner = MotionPlanner(config)
    planner.engage(
        (0.0, 0.0, 0.0),
        IDENTITY_Q,
        (300.0, 0.0, 400.0, 180.0, 0.0, 0.0),
    )

    with pytest.raises(ValueError, match="within max_step_mm"):
        planner.target_for((0.0, 0.0, -1.0), IDENTITY_Q, step_limit_mm=1.1)


def test_invalid_timing_config_is_rejected(tmp_path) -> None:
    config = replace(_web_config(tmp_path), pose_timeout_s=0.001)
    with pytest.raises(ValueError):
        config.validate()


# --- Quaternion primitives -------------------------------------------


def test_quat_multiply_identity() -> None:
    q = (0.1, 0.2, 0.3, math.sqrt(1 - 0.14))
    assert quat_multiply(IDENTITY_Q, q) == pytest.approx(q)
    assert quat_multiply(q, IDENTITY_Q) == pytest.approx(q)


def test_quat_multiply_composes_two_90_degree_rotations() -> None:
    # 90 deg about X then 90 deg about X == 180 deg about X.
    q90 = axis_angle_to_quat((1.0, 0.0, 0.0), math.radians(90))
    q180 = quat_multiply(q90, q90)
    axis, angle = quat_axis_angle(q180)
    assert math.degrees(angle) == pytest.approx(180.0)
    assert axis == pytest.approx((1.0, 0.0, 0.0))


def test_quat_conjugate_is_inverse_for_unit_quaternion() -> None:
    q = quat_normalize((0.1, 0.2, 0.3, 0.9))
    result = quat_multiply(q, quat_conjugate(q))
    assert result == pytest.approx(IDENTITY_Q)


def test_quat_delta_round_trip() -> None:
    origin = axis_angle_to_quat((0.0, 1.0, 0.0), math.radians(20))
    current = axis_angle_to_quat((0.0, 1.0, 0.0), math.radians(50))
    delta = quat_delta(origin, current)
    rebuilt = quat_normalize(quat_multiply(delta, origin))
    dot = abs(quat_dot(rebuilt, current))
    assert dot == pytest.approx(1.0, abs=1e-6)


def test_quat_slerp_endpoints_and_midpoint() -> None:
    a = axis_angle_to_quat((0.0, 0.0, 1.0), 0.0)
    b = axis_angle_to_quat((0.0, 0.0, 1.0), math.radians(90))
    assert quat_slerp(a, b, 0.0) == pytest.approx(a)
    assert abs(quat_dot(quat_slerp(a, b, 1.0), b)) == pytest.approx(1.0, abs=1e-6)
    mid = quat_slerp(a, b, 0.5)
    _, angle = quat_axis_angle(quat_delta(a, mid))
    assert math.degrees(angle) == pytest.approx(45.0, abs=1e-4)


def test_euler_round_trip_simple_cases() -> None:
    for rx, ry, rz in [(0.0, 0.0, 0.0), (30.0, 0.0, 0.0), (0.0, 20.0, 0.0), (0.0, 0.0, -40.0), (10.0, -15.0, 25.0)]:
        q = euler_deg_to_quat(rx, ry, rz)
        out_rx, out_ry, out_rz = quat_to_euler_deg(q)
        assert (out_rx, out_ry, out_rz) == pytest.approx((rx, ry, rz), abs=1e-6)


def test_euler_round_trip_gimbal_lock_is_finite_and_quaternion_equivalent() -> None:
    q = euler_deg_to_quat(10.0, 90.0, 20.0)
    rx, ry, rz = quat_to_euler_deg(q)
    assert all(math.isfinite(v) for v in (rx, ry, rz))
    # Re-encoding the extracted Euler angles must reproduce the same rotation
    # even though the individual rx/rz split is not unique at ry=90.
    rebuilt = euler_deg_to_quat(rx, ry, rz)
    assert abs(quat_dot(rebuilt, q)) == pytest.approx(1.0, abs=1e-6)


def test_remap_quat_vr_to_robot_preserves_angle_and_swaps_axis() -> None:
    q = axis_angle_to_quat((0.0, 1.0, 0.0), math.radians(30))  # VR "up" axis
    remapped = remap_quat_vr_to_robot(q)
    axis, angle = quat_axis_angle(remapped)
    assert math.degrees(angle) == pytest.approx(30.0)
    assert axis == pytest.approx((0.0, 0.0, 1.0), abs=1e-6)


def test_clamp_angular_step_limits_large_rotation() -> None:
    current = IDENTITY_Q
    desired = axis_angle_to_quat((1.0, 0.0, 0.0), math.radians(10))
    clamped = clamp_angular_step(current, desired, 2.0)
    _, angle = quat_axis_angle(quat_delta(current, clamped))
    assert math.degrees(angle) == pytest.approx(2.0, abs=1e-6)


def test_clamp_angular_step_passes_through_small_rotation() -> None:
    current = IDENTITY_Q
    desired = axis_angle_to_quat((1.0, 0.0, 0.0), math.radians(0.5))
    assert clamp_angular_step(current, desired, 2.0) == pytest.approx(desired)


def test_clamp_orientation_deviation_bounds_total_offset() -> None:
    origin = IDENTITY_Q
    desired = axis_angle_to_quat((0.0, 0.0, 1.0), math.radians(45))
    clamped = clamp_orientation_deviation(origin, desired, 20.0)
    _, angle = quat_axis_angle(quat_delta(origin, clamped))
    assert math.degrees(angle) == pytest.approx(20.0, abs=1e-6)


def test_compose_delta_orientation_world_frame() -> None:
    origin = axis_angle_to_quat((0.0, 0.0, 1.0), math.radians(10))
    delta = axis_angle_to_quat((0.0, 0.0, 1.0), math.radians(5))
    composed = compose_delta_orientation(origin, delta)
    _, angle = quat_axis_angle(composed)
    assert math.degrees(angle) == pytest.approx(15.0)


# --- MotionPlanner orientation behavior --------------------------------


def test_orientation_disabled_preserves_existing_position_only_behavior(tmp_path) -> None:
    config = _web_config(
        tmp_path,
        ema_alpha=1.0,
        max_step_mm=1000.0,
        workspace=WorkspaceBounds(x=(-10, 310), y=(-20, 20), z=(50, 450)),
    )
    assert config.orientation.enabled is False
    planner = MotionPlanner(config)
    origin = (300.0, 0.0, 400.0, 180.0, 0.0, 0.0)
    non_identity_orientation = axis_angle_to_quat((0.0, 1.0, 0.0), math.radians(45))
    planner.engage((0.0, 0.0, 0.0), non_identity_orientation, origin)
    target = planner.target_for(
        (0.0, 1.0, -1.0), axis_angle_to_quat((1.0, 0.0, 0.0), math.radians(30))
    )
    assert target == (300, -20, 450, 180, 0, 0)


def test_orientation_enabled_zero_rotation_matches_origin(tmp_path) -> None:
    config = _web_config(
        tmp_path,
        orientation=OrientationConfig(enabled=True, ema_alpha=1.0),
    )
    planner = MotionPlanner(config)
    origin = (300.0, 0.0, 400.0, 12.0, -5.0, 8.0)
    vr_orientation = axis_angle_to_quat((0.0, 1.0, 0.0), math.radians(15))
    planner.engage((0.0, 0.0, 0.0), vr_orientation, origin)
    target = planner.target_for((0.0, 0.0, 0.0), vr_orientation)
    assert target[3:] == pytest.approx((12.0, -5.0, 8.0), abs=1e-6)


def test_orientation_enabled_single_axis_rotation_is_remapped(tmp_path) -> None:
    config = _web_config(
        tmp_path,
        orientation=OrientationConfig(
            enabled=True, ema_alpha=1.0, max_step_deg=90.0, max_deviation_deg=90.0
        ),
    )
    planner = MotionPlanner(config)
    origin = (300.0, 0.0, 400.0, 0.0, 0.0, 0.0)
    vr_origin_q = IDENTITY_Q
    planner.engage((0.0, 0.0, 0.0), vr_origin_q, origin)

    vr_rotated_q = axis_angle_to_quat((0.0, 1.0, 0.0), math.radians(20))
    target = planner.target_for(
        (0.0, 0.0, 0.0),
        vr_rotated_q,
        angular_step_limit_deg=90.0,
    )
    target_q = euler_deg_to_quat(*target[3:])
    axis, angle = quat_axis_angle(target_q)
    assert math.degrees(angle) == pytest.approx(20.0, abs=1e-3)
    assert axis == pytest.approx((0.0, 0.0, 1.0), abs=1e-3)


def test_orientation_angular_step_clamp_limits_single_tick(tmp_path) -> None:
    config = _web_config(
        tmp_path,
        orientation=OrientationConfig(
            enabled=True, ema_alpha=1.0, max_step_deg=1.0, max_deviation_deg=90.0
        ),
    )
    planner = MotionPlanner(config)
    origin = (300.0, 0.0, 400.0, 0.0, 0.0, 0.0)
    planner.engage((0.0, 0.0, 0.0), IDENTITY_Q, origin)

    big_rotation = axis_angle_to_quat((0.0, 1.0, 0.0), math.radians(45))
    target = planner.target_for((0.0, 0.0, 0.0), big_rotation, angular_step_limit_deg=1.0)
    target_q = euler_deg_to_quat(*target[3:])
    _, angle = quat_axis_angle(quat_delta(euler_deg_to_quat(0, 0, 0), target_q))
    assert math.degrees(angle) <= 1.0 + 1e-6


def test_orientation_deviation_clamp_bounds_sustained_rotation(tmp_path) -> None:
    config = _web_config(
        tmp_path,
        orientation=OrientationConfig(
            enabled=True,
            ema_alpha=1.0,
            max_step_deg=45.0,
            max_angular_velocity_deg_s=1000.0,
            max_deviation_deg=10.0,
        ),
    )
    planner = MotionPlanner(config)
    origin = (300.0, 0.0, 400.0, 0.0, 0.0, 0.0)
    planner.engage((0.0, 0.0, 0.0), IDENTITY_Q, origin)

    held_rotation = axis_angle_to_quat((0.0, 1.0, 0.0), math.radians(45))
    target = None
    for _ in range(50):
        target = planner.target_for(
            (0.0, 0.0, 0.0), held_rotation, angular_step_limit_deg=45.0
        )
    target_q = euler_deg_to_quat(*target[3:])
    _, angle = quat_axis_angle(quat_delta(euler_deg_to_quat(0, 0, 0), target_q))
    assert math.degrees(angle) <= 10.0 + 1e-3


def test_orientation_handles_near_180_degree_rotation(tmp_path) -> None:
    config = _web_config(
        tmp_path,
        orientation=OrientationConfig(enabled=True, ema_alpha=1.0, max_step_deg=180.0, max_deviation_deg=90.0),
    )
    planner = MotionPlanner(config)
    origin = (300.0, 0.0, 400.0, 0.0, 0.0, 0.0)
    planner.engage((0.0, 0.0, 0.0), IDENTITY_Q, origin)

    near_flip = axis_angle_to_quat((1.0, 0.0, 0.0), math.radians(179.9))
    target = planner.target_for((0.0, 0.0, 0.0), near_flip, angular_step_limit_deg=180.0)
    assert all(math.isfinite(v) for v in target)
