from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import Any, Mapping

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class WorkspaceBounds:
    x: tuple[float, float] = (-600.0, 600.0)
    y: tuple[float, float] = (-600.0, 600.0)
    z: tuple[float, float] = (50.0, 700.0)

    def validate(self) -> None:
        for axis, bounds in (("x", self.x), ("y", self.y), ("z", self.z)):
            if (
                len(bounds) != 2
                or not all(isfinite(value) for value in bounds)
                or bounds[0] >= bounds[1]
            ):
                raise ValueError(f"workspace {axis} bounds must be increasing")


@dataclass(frozen=True)
class TeleopConfig:
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8443
    dry_run: bool = True
    robot_ip: str | None = None
    sdk_path: Path = field(
        default_factory=lambda: PROJECT_ROOT / "fairino-python-sdk-main" / "linux"
    )
    web_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "web")
    tls_cert_path: Path | None = field(
        default_factory=lambda: PROJECT_ROOT / "certs" / "cert.pem"
    )
    tls_key_path: Path | None = field(
        default_factory=lambda: PROJECT_ROOT / "certs" / "key.pem"
    )
    servo_period_s: float = 0.008
    pose_timeout_s: float = 0.100
    worker_watchdog_s: float = 0.500
    worker_startup_timeout_s: float = 10.0
    graceful_shutdown_s: float = 2.0
    status_hz: float = 10.0
    position_scale: float = 500.0
    ema_alpha: float = 0.25
    max_step_mm: float = 1.5
    workspace: WorkspaceBounds = field(default_factory=WorkspaceBounds)
    exaxis_default: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    max_ws_message_bytes: int = 4096
    allowed_origins: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("log_level must be DEBUG, INFO, WARNING, or ERROR")
        if not self.host:
            raise ValueError("host must not be empty")
        if not (1 <= self.port <= 65535):
            raise ValueError("port must be in [1, 65535]")
        if not self.dry_run and not self.robot_ip:
            raise ValueError("robot_ip is required outside dry-run")
        if not isfinite(self.servo_period_s) or self.servo_period_s <= 0:
            raise ValueError("servo_period_s must be positive")
        if (
            not isfinite(self.pose_timeout_s)
            or self.pose_timeout_s < self.servo_period_s
        ):
            raise ValueError("pose_timeout_s must be at least one servo period")
        if (
            not isfinite(self.worker_watchdog_s)
            or self.worker_watchdog_s <= self.pose_timeout_s
        ):
            raise ValueError("worker_watchdog_s must exceed pose_timeout_s")
        if (
            not isfinite(self.worker_startup_timeout_s)
            or self.worker_startup_timeout_s <= self.worker_watchdog_s
        ):
            raise ValueError("worker_startup_timeout_s must exceed worker_watchdog_s")
        if (
            not isfinite(self.graceful_shutdown_s)
            or self.graceful_shutdown_s <= 0
        ):
            raise ValueError("graceful_shutdown_s must be positive")
        if not isfinite(self.status_hz) or not (1.0 <= self.status_hz <= 60.0):
            raise ValueError("status_hz must be in [1, 60]")
        if not isfinite(self.position_scale) or self.position_scale <= 0:
            raise ValueError("position_scale must be positive")
        if not isfinite(self.ema_alpha) or not (0.0 < self.ema_alpha <= 1.0):
            raise ValueError("ema_alpha must be in (0, 1]")
        if not isfinite(self.max_step_mm) or self.max_step_mm <= 0:
            raise ValueError("max_step_mm must be positive")
        if len(self.exaxis_default) != 4:
            raise ValueError("exaxis_default must contain four values")
        if not all(isfinite(value) for value in self.exaxis_default):
            raise ValueError("exaxis_default values must be finite")
        if self.max_ws_message_bytes < 256:
            raise ValueError("max_ws_message_bytes is too small")
        if not self.web_dir.joinpath("index.html").is_file():
            raise ValueError(f"WebXR UI not found: {self.web_dir / 'index.html'}")
        if not self.dry_run and not self.sdk_path.joinpath("fairino", "Robot.py").is_file():
            raise ValueError(f"Fairino Linux SDK not found: {self.sdk_path}")
        if (self.tls_cert_path is None) != (self.tls_key_path is None):
            raise ValueError("TLS certificate and key must be configured together")
        if self.tls_cert_path is not None:
            if not self.tls_cert_path.is_file():
                raise ValueError(f"TLS certificate not found: {self.tls_cert_path}")
            if not self.tls_key_path or not self.tls_key_path.is_file():
                raise ValueError(f"TLS key not found: {self.tls_key_path}")
        self.workspace.validate()

    @classmethod
    def from_yaml(cls, path: Path, *, validate: bool = True) -> TeleopConfig:
        config_path = path.expanduser().resolve()
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"unable to read config file {config_path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid YAML in {config_path}: {exc}") from exc

        root = _mapping(raw, "config")
        _reject_unknown(
            root,
            {"runtime", "server", "tls", "robot", "timing", "motion"},
            "config",
        )
        runtime = _section(root, "runtime")
        server = _section(root, "server")
        tls = _section(root, "tls")
        robot = _section(root, "robot")
        timing = _section(root, "timing")
        motion = _section(root, "motion")
        workspace = _section(motion, "workspace")

        _reject_unknown(runtime, {"dry_run", "log_level", "status_hz"}, "runtime")
        _reject_unknown(
            server,
            {"host", "port", "web_dir", "max_ws_message_bytes", "allowed_origins"},
            "server",
        )
        _reject_unknown(tls, {"enabled", "cert_path", "key_path"}, "tls")
        _reject_unknown(robot, {"ip", "sdk_path", "exaxis_default"}, "robot")
        _reject_unknown(
            timing,
            {
                "servo_period_s",
                "pose_timeout_s",
                "worker_watchdog_s",
                "worker_startup_timeout_s",
                "graceful_shutdown_s",
            },
            "timing",
        )
        _reject_unknown(
            motion,
            {"position_scale", "ema_alpha", "max_step_mm", "workspace"},
            "motion",
        )
        _reject_unknown(workspace, {"x", "y", "z"}, "motion.workspace")

        defaults = cls()
        base_dir = config_path.parent
        tls_enabled = _boolean(tls.get("enabled", True), "tls.enabled")
        cert_path = (
            _path(tls.get("cert_path", defaults.tls_cert_path), "tls.cert_path", base_dir)
            if tls_enabled
            else None
        )
        key_path = (
            _path(tls.get("key_path", defaults.tls_key_path), "tls.key_path", base_dir)
            if tls_enabled
            else None
        )
        robot_ip = robot.get("ip", defaults.robot_ip)
        if robot_ip is not None:
            robot_ip = _string(robot_ip, "robot.ip")

        config = cls(
            log_level=_choice(
                runtime.get("log_level", defaults.log_level),
                "runtime.log_level",
                {"DEBUG", "INFO", "WARNING", "ERROR"},
            ),
            host=_string(server.get("host", defaults.host), "server.host"),
            port=_integer(server.get("port", defaults.port), "server.port"),
            dry_run=_boolean(
                runtime.get("dry_run", defaults.dry_run), "runtime.dry_run"
            ),
            robot_ip=robot_ip,
            sdk_path=_path(
                robot.get("sdk_path", defaults.sdk_path), "robot.sdk_path", base_dir
            ),
            web_dir=_path(
                server.get("web_dir", defaults.web_dir), "server.web_dir", base_dir
            ),
            tls_cert_path=cert_path,
            tls_key_path=key_path,
            servo_period_s=_number(
                timing.get("servo_period_s", defaults.servo_period_s),
                "timing.servo_period_s",
            ),
            pose_timeout_s=_number(
                timing.get("pose_timeout_s", defaults.pose_timeout_s),
                "timing.pose_timeout_s",
            ),
            worker_watchdog_s=_number(
                timing.get("worker_watchdog_s", defaults.worker_watchdog_s),
                "timing.worker_watchdog_s",
            ),
            worker_startup_timeout_s=_number(
                timing.get(
                    "worker_startup_timeout_s", defaults.worker_startup_timeout_s
                ),
                "timing.worker_startup_timeout_s",
            ),
            graceful_shutdown_s=_number(
                timing.get("graceful_shutdown_s", defaults.graceful_shutdown_s),
                "timing.graceful_shutdown_s",
            ),
            status_hz=_number(
                runtime.get("status_hz", defaults.status_hz), "runtime.status_hz"
            ),
            position_scale=_number(
                motion.get("position_scale", defaults.position_scale),
                "motion.position_scale",
            ),
            ema_alpha=_number(
                motion.get("ema_alpha", defaults.ema_alpha), "motion.ema_alpha"
            ),
            max_step_mm=_number(
                motion.get("max_step_mm", defaults.max_step_mm),
                "motion.max_step_mm",
            ),
            workspace=WorkspaceBounds(
                x=_bounds(workspace.get("x", defaults.workspace.x), "motion.workspace.x"),
                y=_bounds(workspace.get("y", defaults.workspace.y), "motion.workspace.y"),
                z=_bounds(workspace.get("z", defaults.workspace.z), "motion.workspace.z"),
            ),
            exaxis_default=_four_numbers(
                robot.get("exaxis_default", defaults.exaxis_default),
                "robot.exaxis_default",
            ),
            max_ws_message_bytes=_integer(
                server.get(
                    "max_ws_message_bytes", defaults.max_ws_message_bytes
                ),
                "server.max_ws_message_bytes",
            ),
            allowed_origins=_strings(
                server.get("allowed_origins", defaults.allowed_origins),
                "server.allowed_origins",
            ),
        )
        if validate:
            config.validate()
        return config


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise ValueError(f"{name} must be a YAML mapping")
    return value


def _section(parent: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = parent.get(name, {})
    return _mapping(value, name)


def _reject_unknown(
    value: Mapping[str, Any], allowed: set[str], name: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown {name} key(s): {', '.join(unknown)}")


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _choice(value: Any, name: str, choices: set[str]) -> str:
    result = _string(value, name).upper()
    if result not in choices:
        raise ValueError(f"{name} must be one of {', '.join(sorted(choices))}")
    return result


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    return float(value)


def _path(value: Any, name: str, base_dir: Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise ValueError(f"{name} must be a path string")
    path = Path(_string(str(value), name)).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _sequence(value: Any, name: str) -> list[Any] | tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a YAML sequence")
    return value


def _bounds(value: Any, name: str) -> tuple[float, float]:
    values = _sequence(value, name)
    if len(values) != 2:
        raise ValueError(f"{name} must contain two numbers")
    return (_number(values[0], name), _number(values[1], name))


def _four_numbers(
    value: Any, name: str
) -> tuple[float, float, float, float]:
    values = _sequence(value, name)
    if len(values) != 4:
        raise ValueError(f"{name} must contain four numbers")
    return tuple(_number(item, name) for item in values)  # type: ignore[return-value]


def _strings(value: Any, name: str) -> tuple[str, ...]:
    return tuple(_string(item, name) for item in _sequence(value, name))
