from __future__ import annotations

from pathlib import Path

import pytest

from teleop.config import GripperConfig, TeleopConfig
from teleop.__main__ import build_parser, resolve_config


def write_config(path: Path, extra: str = "") -> Path:
    web_dir = path.parent / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")
    path.write_text(
        """
runtime:
  dry_run: true
  log_level: warning
server:
  host: 127.0.0.1
  port: 9000
  web_dir: web
  allowed_origins:
    - https://quest.local
tls:
  enabled: false
robot:
  ip: 192.168.58.2
  sdk_path: sdk/linux
timing:
  servo_transition_window_s: 2.0
  servo_transition_limit: 6
motion:
  position_scale: 250
  max_velocity_mm_s: 40
  workspace:
    x: [-100, 100]
    y: [-200, 200]
    z: [100, 500]
gripper:
  enabled: true
  index: 1
  open_position: 5
  closed_position: 95
  velocity: 30
  force: 40
  command_max_time_ms: 2000
  action_timeout_s: 3.0
  poll_period_s: 0.1
"""
        + extra,
        encoding="utf-8",
    )
    return path


def test_yaml_loads_values_and_resolves_relative_paths(tmp_path: Path) -> None:
    config = TeleopConfig.from_yaml(write_config(tmp_path / "config.yaml"))

    assert config.dry_run is True
    assert config.log_level == "WARNING"
    assert config.host == "127.0.0.1"
    assert config.port == 9000
    assert config.web_dir == (tmp_path / "web").resolve()
    assert config.sdk_path == (tmp_path / "sdk/linux").resolve()
    assert config.tls_cert_path is None
    assert config.tls_key_path is None
    assert config.servo_transition_window_s == 2.0
    assert config.servo_transition_limit == 6
    assert config.position_scale == 250.0
    assert config.max_velocity_mm_s == 40.0
    assert config.workspace.z == (100.0, 500.0)
    assert config.allowed_origins == ("https://quest.local",)
    assert config.gripper.enabled is True
    assert config.gripper.closed_position == 95
    assert config.gripper.poll_period_s == 0.1


def test_yaml_rejects_unknown_keys(tmp_path: Path) -> None:
    path = write_config(tmp_path / "config.yaml")
    path.write_text(
        path.read_text(encoding="utf-8") + "\nunknown_section: {}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown config key"):
        TeleopConfig.from_yaml(path)


def test_yaml_requires_real_boolean_values(tmp_path: Path) -> None:
    path = write_config(tmp_path / "config.yaml")
    text = path.read_text(encoding="utf-8").replace(
        "dry_run: true", 'dry_run: "true"'
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="runtime.dry_run must be a boolean"):
        TeleopConfig.from_yaml(path)


def test_yaml_rejects_non_finite_motion_limits(tmp_path: Path) -> None:
    path = write_config(tmp_path / "config.yaml")
    text = path.read_text(encoding="utf-8").replace(
        "position_scale: 250", "position_scale: .nan"
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="position_scale must be positive"):
        TeleopConfig.from_yaml(path)


def test_cli_no_tls_override_is_applied_before_path_validation(
    tmp_path: Path,
) -> None:
    path = write_config(tmp_path / "config.yaml")
    text = path.read_text(encoding="utf-8").replace(
        "enabled: false",
        "enabled: true\n  cert_path: missing/cert.pem\n  key_path: missing/key.pem",
    )
    path.write_text(text, encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(["--config", str(path), "--no-tls"])

    config = resolve_config(parser, args)

    assert config.tls_cert_path is None
    assert config.tls_key_path is None


def test_cli_hardware_mode_requires_explicit_confirmation(tmp_path: Path) -> None:
    path = write_config(tmp_path / "config.yaml")
    parser = build_parser()
    args = parser.parse_args(
        ["--config", str(path), "--robot", "192.168.58.2", "--no-tls"]
    )

    with pytest.raises(SystemExit):
        resolve_config(parser, args)


def test_gripper_timeout_must_cover_controller_command() -> None:
    config = TeleopConfig(
        gripper=GripperConfig(
            command_max_time_ms=3000,
            action_timeout_s=2.0,
        )
    )

    with pytest.raises(ValueError, match="must cover command_max_time_ms"):
        config.validate()
