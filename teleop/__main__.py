from __future__ import annotations

import argparse
import logging
import ssl
from dataclasses import replace
from pathlib import Path

from aiohttp import web

from teleop.app import create_app
from teleop.config import PROJECT_ROOT, TeleopConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quest 3 to Fairino FR5 teleop bridge")
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config.yaml",
        help="YAML configuration file",
    )
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--robot", dest="robot_ip")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="force fake robot mode even if YAML enables hardware",
    )
    parser.add_argument(
        "--confirm-hardware",
        action="store_true",
        help="required together with --robot to permit real hardware mode",
    )
    parser.add_argument(
        "--sdk-path",
        type=Path,
    )
    parser.add_argument("--scale", type=float)
    parser.add_argument("--pose-timeout-ms", type=float)
    parser.add_argument("--no-tls", action="store_true", help="localhost/test only")
    parser.add_argument(
        "--cert", type=Path
    )
    parser.add_argument(
        "--key", type=Path
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser


def resolve_config(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> TeleopConfig:
    try:
        config = TeleopConfig.from_yaml(args.config, validate=False)
    except ValueError as exc:
        parser.error(str(exc))

    overrides = {
        "host": args.host,
        "port": args.port,
        "robot_ip": args.robot_ip,
        "sdk_path": args.sdk_path.resolve() if args.sdk_path else None,
        "position_scale": args.scale,
        "pose_timeout_s": (
            args.pose_timeout_ms / 1000.0
            if args.pose_timeout_ms is not None
            else None
        ),
        "log_level": args.log_level,
    }
    config = replace(
        config,
        **{key: value for key, value in overrides.items() if value is not None},
    )
    if args.robot_ip:
        config = replace(config, dry_run=False)
    if args.dry_run:
        config = replace(config, dry_run=True)
    if args.no_tls:
        config = replace(config, tls_cert_path=None, tls_key_path=None)
    else:
        if args.cert is not None:
            config = replace(config, tls_cert_path=args.cert.resolve())
        if args.key is not None:
            config = replace(config, tls_key_path=args.key.resolve())
    if not config.dry_run and not args.confirm_hardware:
        parser.error("hardware mode requires --confirm-hardware")
    try:
        config.validate()
    except ValueError as exc:
        parser.error(str(exc))
    return config


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = resolve_config(parser, args)

    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format=(
            "%(asctime)s [pid=%(process)d] [%(processName)s] "
            "[%(levelname)s] %(message)s"
        ),
    )

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
