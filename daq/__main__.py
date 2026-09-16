"""Command line entry point: python -m daq"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
from pathlib import Path

import uvicorn

from .config import ConfigError, load_config
from .server import create_app
from .sources import list_serial_ports


def _lan_address() -> str | None:
    """Best guess at this machine's LAN address, without sending anything."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 1))  # TEST-NET-1: routable lookup, no traffic
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m daq",
        description="Run the cold-plate loop DAQ server and dashboard.",
    )
    parser.add_argument(
        "--config", default="config.yaml", type=Path, help="path to config.yaml"
    )
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default 8000)")
    parser.add_argument(
        "--lan",
        action="store_true",
        help="bind all interfaces so other machines on the network can view the "
        "dashboard. There is no authentication, so only use this on a trusted "
        "lab network.",
    )
    parser.add_argument(
        "--source",
        choices=["simulator", "serial"],
        help="override acquisition.source from config.yaml",
    )
    parser.add_argument(
        "--serial-port", help="override acquisition.serial.port, e.g. COM4"
    )
    parser.add_argument(
        "--list-ports", action="store_true", help="print detected serial ports and exit"
    )
    parser.add_argument("--log-level", default="info", help="uvicorn log level")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.list_ports:
        ports = list_serial_ports()
        if not ports:
            print("No serial ports detected.")
            return 0
        print(f"{'PORT':<12} {'LIKELY BOARD':<14} DESCRIPTION")
        for port in ports:
            flag = "yes" if port["likelyBoard"] else "-"
            print(f"{port['device']:<12} {flag:<14} {port['description']}")
        return 0

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error in {args.config}:\n  {exc}", file=sys.stderr)
        return 2

    if args.source:
        config.source = args.source
    if args.serial_port:
        config.serial_port = args.serial_port
        config.source = "serial"

    config.data_dir.mkdir(parents=True, exist_ok=True)
    host = "0.0.0.0" if args.lan else "127.0.0.1"

    measured = len(config.measured)
    print()
    print(f"  {config.name}")
    print(f"  {measured} measured + {len(config.derived)} derived channels"
          f"  |  source: {config.source}  |  {config.rate_hz:g} Hz")
    print(f"  runs -> {config.data_dir}")
    print()
    print(f"  Dashboard   http://localhost:{args.port}")
    if args.lan:
        lan = _lan_address()
        if lan:
            print(f"  On network  http://{lan}:{args.port}   (no authentication)")
    else:
        print(f"  Add --lan to let other machines on the network view it.")
    print()

    uvicorn.run(
        create_app(config),
        host=host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
