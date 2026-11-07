"""Command line interface: ``nxremote serve | setup-bluez | doctor | forget | token``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .store import PairingStore, default_state_dir, load_or_create_token


def _colour(value: str) -> bytes:
    v = value.lstrip("#")
    if len(v) != 6:
        raise argparse.ArgumentTypeError("colour must be 6 hex digits, e.g. 82827F")
    try:
        return bytes.fromhex(v)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nxremote", description=__doc__)
    p.add_argument("--version", action="version", version=f"nxremote {__version__}")
    p.add_argument("--state-dir", type=Path, default=None, help="where pairings and the API token are stored")
    p.add_argument("--log-level", default=os.environ.get("NXREMOTE_LOG_LEVEL", "info"))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the controller + remote API")
    s.add_argument("--host", default=os.environ.get("NXREMOTE_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("NXREMOTE_PORT", "8000")))
    s.add_argument("--backend", choices=["bluetooth", "virtual"], default="bluetooth",
                   help="'virtual' simulates a Switch in-process (development, no Bluetooth needed)")
    s.add_argument("--adapter", default=os.environ.get("NXREMOTE_ADAPTER", "hci0"))
    s.add_argument("--token", default=os.environ.get("NXREMOTE_TOKEN"), help="API token (default: generated and stored)")
    s.add_argument("--no-auth", action="store_true", help="disable authentication (only on trusted networks!)")
    s.add_argument("--no-autostart", action="store_true", help="do not connect until POST /v1/connection/start")
    s.add_argument("--body-colour", type=_colour, default=bytes.fromhex("323232"))
    s.add_argument("--button-colour", type=_colour, default=bytes.fromhex("FFFFFF"))
    s.add_argument("--auto-rumble", action="store_true", help="virtual backend: rumble when A is pressed (demo)")

    b = sub.add_parser("setup-bluez", help="disable BlueZ's input plugin (needed once; requires root)")
    b.add_argument("--restore", action="store_true", help="undo the change")
    b.add_argument("--temporary", action="store_true", help="only until reboot (writes to /run)")

    d = sub.add_parser("doctor", help="check that this host can act as a controller")
    d.add_argument("--adapter", default="hci0")

    f = sub.add_parser("forget", help="forget remembered consoles")
    f.add_argument("address", nargs="?")
    f.add_argument("--adapter", default="hci0")

    sub.add_parser("token", help="print the API token")
    return p


def cmd_serve(args, state_dir: Path) -> int:
    import uvicorn

    from .api.app import create_app
    from .core import ControllerService
    from .hub import InputHub

    store = PairingStore(state_dir)
    hub = InputHub()
    if args.backend == "virtual":
        from .virtual import VirtualConnector

        connector = VirtualConnector(auto_rumble=args.auto_rumble)
    else:
        from .bt.connector import BluetoothConnector

        connector = BluetoothConnector(args.adapter)

    service = ControllerService(
        connector, hub, store, body_colour=args.body_colour, button_colour=args.button_colour
    )

    token: Optional[str] = None
    if args.no_auth:
        if args.host not in ("127.0.0.1", "::1", "localhost"):
            logging.warning("authentication is disabled and the API listens on %s: anyone on the network can play!", args.host)
    else:
        token = args.token or load_or_create_token(state_dir)
        if not args.token:
            logging.info("API token stored in %s (show it with `nxremote token`)", state_dir / "token")

    app = create_app(service, hub, token=token, autostart=not args.no_autostart)
    # Realtime input is many tiny frames: disable permessage-deflate (compressing
    # ~100-byte packets only adds CPU and latency) and keep Nagle off (see the WS
    # endpoint, which sets TCP_NODELAY on each accepted socket).
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level.lower(),
        ws_ping_interval=20,
        ws_per_message_deflate=False,
    )
    return 0


def cmd_setup_bluez(args) -> int:
    from .bt import setup

    try:
        if args.restore:
            removed = setup.restore()
            print("removed:", ", ".join(map(str, removed)) or "nothing to remove")
        else:
            path = setup.install(temporary=args.temporary)
            print(f"wrote {path}; bluetoothd restarted with the input plugin disabled")
            print("note: Bluetooth keyboards/mice will not work on this host until you run `nxremote setup-bluez --restore`")
    except (PermissionError, RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_doctor(args) -> int:
    from .bt import setup

    ok_all = True
    for name, ok, detail in setup.doctor(args.adapter):
        ok_all &= ok
        print(f"[{'ok' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return 0 if ok_all else 1


def cmd_forget(args, state_dir: Path) -> int:
    from .bt.connector import BluetoothConnector

    removed = set(PairingStore(state_dir).forget(args.address))
    try:
        removed |= set(BluetoothConnector(args.adapter).forget(args.address))
    except Exception as e:
        print(f"warning: BlueZ cleanup failed: {e}", file=sys.stderr)
    print("removed:", ", ".join(sorted(removed)) or "nothing")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    state_dir = args.state_dir or default_state_dir()
    if args.cmd == "serve":
        return cmd_serve(args, state_dir)
    if args.cmd == "setup-bluez":
        return cmd_setup_bluez(args)
    if args.cmd == "doctor":
        return cmd_doctor(args)
    if args.cmd == "forget":
        return cmd_forget(args, state_dir)
    if args.cmd == "token":
        print(load_or_create_token(state_dir))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
