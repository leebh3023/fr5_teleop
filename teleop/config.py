from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class WorkspaceBounds:
    x: tuple[float, float] = (-600.0, 600.0)
    y: tuple[float, float] = (-600.0, 600.0)
    z: tuple[float, float] = (50.0, 700.0)

    def validate(self) -> None:
        for axis, bounds in (("x", self.x), ("y", self.y), ("z", self.z)):
            if len(bounds) != 2 or bounds[0] >= bounds[1]:
                raise ValueError(f"workspace {axis} bounds must be increasing")


@dataclass(frozen=True)
class TeleopConfig:
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
        if not (1 <= self.port <= 65535):
            raise ValueError("port must be in [1, 65535]")
        if not self.dry_run and not self.robot_ip:
            raise ValueError("robot_ip is required outside dry-run")
        if self.servo_period_s <= 0:
            raise ValueError("servo_period_s must be positive")
        if self.pose_timeout_s < self.servo_period_s:
            raise ValueError("pose_timeout_s must be at least one servo period")
        if self.worker_watchdog_s <= self.pose_timeout_s:
            raise ValueError("worker_watchdog_s must exceed pose_timeout_s")
        if self.worker_startup_timeout_s <= self.worker_watchdog_s:
            raise ValueError("worker_startup_timeout_s must exceed worker_watchdog_s")
        if self.graceful_shutdown_s <= 0:
            raise ValueError("graceful_shutdown_s must be positive")
        if not (1.0 <= self.status_hz <= 60.0):
            raise ValueError("status_hz must be in [1, 60]")
        if self.position_scale <= 0:
            raise ValueError("position_scale must be positive")
        if not (0.0 < self.ema_alpha <= 1.0):
            raise ValueError("ema_alpha must be in (0, 1]")
        if self.max_step_mm <= 0:
            raise ValueError("max_step_mm must be positive")
        if len(self.exaxis_default) != 4:
            raise ValueError("exaxis_default must contain four values")
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
