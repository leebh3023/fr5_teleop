from dataclasses import replace

import pytest

from teleop.config import TeleopConfig, WorkspaceBounds
from teleop.control_math import MotionPlanner, clamp_step, vr_to_robot_delta


def test_vr_axis_mapping() -> None:
    assert vr_to_robot_delta((1.0, 2.0, 3.0), 500.0) == (
        -1500.0,
        -500.0,
        1000.0,
    )


def test_step_is_limited_by_vector_length() -> None:
    assert clamp_step((3.0, 4.0, 0.0), 2.5) == pytest.approx((1.5, 2.0, 0.0))


def test_engage_resets_previous_target_and_clamps_workspace(tmp_path) -> None:
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    web_dir.joinpath("index.html").write_text("", encoding="utf-8")
    web_dir.joinpath("monitor.html").write_text("", encoding="utf-8")
    config = TeleopConfig(
        web_dir=web_dir,
        tls_cert_path=None,
        tls_key_path=None,
        ema_alpha=1.0,
        max_step_mm=1000.0,
        workspace=WorkspaceBounds(x=(-10, 310), y=(-20, 20), z=(50, 450)),
    )
    planner = MotionPlanner(config)
    origin = (300.0, 0.0, 400.0, 180.0, 0.0, 0.0)
    planner.engage((0.0, 0.0, 0.0), origin)
    assert planner.target_for((0.0, 1.0, -1.0)) == (
        310,
        0,
        450,
        180,
        0,
        0,
    )
    planner.engage((5.0, 5.0, 5.0), origin)
    assert planner.last_target == origin


def test_target_accepts_time_based_step_below_absolute_cap(tmp_path) -> None:
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    web_dir.joinpath("index.html").write_text("", encoding="utf-8")
    web_dir.joinpath("monitor.html").write_text("", encoding="utf-8")
    config = TeleopConfig(
        web_dir=web_dir,
        tls_cert_path=None,
        tls_key_path=None,
        ema_alpha=1.0,
        max_velocity_mm_s=50.0,
        max_step_mm=1.0,
    )
    planner = MotionPlanner(config)
    origin = (300.0, 0.0, 400.0, 180.0, 0.0, 0.0)
    planner.engage((0.0, 0.0, 0.0), origin)

    target = planner.target_for(
        (0.0, 0.0, -1.0),
        step_limit_mm=0.4,
    )

    assert target[:3] == pytest.approx((300.4, 0.0, 400.0))


def test_target_rejects_step_above_absolute_cap(tmp_path) -> None:
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    web_dir.joinpath("index.html").write_text("", encoding="utf-8")
    web_dir.joinpath("monitor.html").write_text("", encoding="utf-8")
    config = TeleopConfig(
        web_dir=web_dir,
        tls_cert_path=None,
        tls_key_path=None,
        max_velocity_mm_s=50.0,
        max_step_mm=1.0,
    )
    planner = MotionPlanner(config)
    planner.engage(
        (0.0, 0.0, 0.0),
        (300.0, 0.0, 400.0, 180.0, 0.0, 0.0),
    )

    with pytest.raises(ValueError, match="within max_step_mm"):
        planner.target_for((0.0, 0.0, -1.0), step_limit_mm=1.1)


def test_invalid_timing_config_is_rejected(tmp_path) -> None:
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    web_dir.joinpath("index.html").write_text("", encoding="utf-8")
    web_dir.joinpath("monitor.html").write_text("", encoding="utf-8")
    config = replace(
        TeleopConfig(web_dir=web_dir, tls_cert_path=None, tls_key_path=None),
        pose_timeout_s=0.001,
    )
    with pytest.raises(ValueError):
        config.validate()
