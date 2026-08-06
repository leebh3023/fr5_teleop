from __future__ import annotations

import argparse
import logging
import ssl
from pathlib import Path

from aiohttp import web

from teleop.app import create_app
from teleop.config import PROJECT_ROOT, TeleopConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quest 3 to Fairino FR5 teleop bridge")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--robot", dest="robot_ip")
    parser.add_argument(
        "--confirm-hardware",
        action="store_true",
        help="required together with --robot to permit real hardware mode",
    )
    parser.add_argument(
        "--sdk-path",
        type=Path,
        default=PROJECT_ROOT / "fairino-python-sdk-main" / "linux",
    )
    parser.add_argument("--scale", type=float, default=500.0)
    parser.add_argument("--pose-timeout-ms", type=float, default=100.0)
    parser.add_argument("--no-tls", action="store_true", help="localhost/test only")
    parser.add_argument(
        "--cert", type=Path, default=PROJECT_ROOT / "certs" / "cert.pem"
    )
    parser.add_argument(
        "--key", type=Path, default=PROJECT_ROOT / "certs" / "key.pem"
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.robot_ip and not args.confirm_hardware:
        parser.error("--robot requires --confirm-hardware")

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(processName)s] [%(levelname)s] %(message)s",
    )
    tls_cert = None if args.no_tls else args.cert
    tls_key = None if args.no_tls else args.key
    config = TeleopConfig(
        host=args.host,
        port=args.port,
        dry_run=args.robot_ip is None,
        robot_ip=args.robot_ip,
        sdk_path=args.sdk_path,
        tls_cert_path=tls_cert,
        tls_key_path=tls_key,
        position_scale=args.scale,
        pose_timeout_s=args.pose_timeout_ms / 1000.0,
    )
    config.validate()

    ssl_context = None
    if config.tls_cert_path and config.tls_key_path:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(config.tls_cert_path, config.tls_key_path)

    mode = "DRY-RUN" if config.dry_run else f"HARDWARE {config.robot_ip}"
    logging.getLogger(__name__).info(
        "starting teleop mode=%s url=%s://%s:%d",
        mode,
        "https" if ssl_context else "http",
        config.host,
        config.port,
    )
    web.run_app(
        create_app(config),
        host=config.host,
        port=config.port,
        ssl_context=ssl_context,
        print=None,
    )


if __name__ == "__main__":
    main()
