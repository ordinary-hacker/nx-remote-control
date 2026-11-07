"""``nxremote-client`` command line: forward a local controller to an nxremote host."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import Optional

from . import __version__
from .mapping import Mapping
from .runner import ForwarderClient
from .sources import InputSource, NullSink, RumbleSink, StaticSource
from .transport import Transport, build_ws_url


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nxremote-client",
        description="Forward a locally-connected game controller over the internet to an nxremote host.",
    )
    p.add_argument("host", nargs="?", help="host as HOST[:PORT] or a full ws(s)://.../URL (default: $NXREMOTE_HOST)")
    p.add_argument("--token", default=os.environ.get("NXREMOTE_TOKEN"), help="API token (default: $NXREMOTE_TOKEN)")
    p.add_argument("--version", action="version", version=f"nxremote-client {__version__}")

    p.add_argument("--source", choices=["sdl", "keyboard"], default="sdl", help="input device (default: sdl gamepad)")
    p.add_argument("--controller", type=int, default=0, help="which gamepad to use when several are attached")
    p.add_argument("--list", action="store_true", help="list detected gamepads and exit")
    p.add_argument("--no-rumble", action="store_true", help="do not play the Switch's rumble on the gamepad")

    p.add_argument("--face", choices=["positional", "labels"], default="positional",
                   help="face-button convention: positional keeps physical layout (default); labels matches printed letters")
    p.add_argument("--swap-ab", action="store_true", help="swap the A/B mapping")
    p.add_argument("--swap-xy", action="store_true", help="swap the X/Y mapping")
    p.add_argument("--deadzone", type=float, default=0.08, help="stick deadzone 0..1 (default 0.08)")
    p.add_argument("--trigger-threshold", type=float, default=0.4, help="analog trigger press point for ZL/ZR (default 0.4)")

    p.add_argument("--rate", type=float, default=120.0, help="poll/send rate in Hz (default 120)")
    p.add_argument("--no-reconnect", action="store_true", help="exit instead of retrying when the connection drops")
    p.add_argument("--keep-on-exit", action="store_true",
                   help="leave inputs held on the host if the client exits (default: host releases them)")
    p.add_argument("--log-level", default=os.environ.get("NXREMOTE_LOG_LEVEL", "info"))
    return p


def _make_source_and_sink(args) -> tuple[InputSource, RumbleSink, Optional[object]]:
    """Return (source, sink, sdl_context-or-None). The context must outlive both."""
    if args.source == "keyboard":
        from .keyboard import KeyboardSource

        return KeyboardSource(), NullSink(), None

    from .sdl_backend import SDLContext, SDLGamepadSource, SDLRumbleSink, SDLUnavailable

    try:
        ctx = SDLContext().__enter__()
    except SDLUnavailable as e:
        raise SystemExit(f"error: {e}")

    mapping = Mapping(
        face=args.face, trigger_threshold=args.trigger_threshold, swap_ab=args.swap_ab, swap_xy=args.swap_xy
    )
    source = SDLGamepadSource(ctx, index=args.controller, mapping=mapping, deadzone=args.deadzone)
    sink: RumbleSink = NullSink() if args.no_rumble else SDLRumbleSink(source)
    return source, sink, ctx


def _list_controllers() -> int:
    from .sdl_backend import SDLContext, SDLUnavailable

    try:
        with SDLContext() as ctx:
            controllers = ctx.list_controllers()
    except SDLUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not controllers:
        print("no game controllers detected")
        return 1
    print("detected controllers:")
    for i, (_, name) in enumerate(controllers):
        print(f"  [{i}] {name}")
    return 0


async def _run(args, source: InputSource, sink: RumbleSink) -> None:
    url = build_ws_url(args.host, token=args.token or "", release_on_close=not args.keep_on_exit)

    def on_connected(hello: dict) -> None:
        ctrl = hello.get("controller", {})
        state = ctrl.get("state", "?")
        logging.getLogger("nxremote-client").info(
            "host connected; Switch link: %s%s", state, "" if ctrl.get("connected") else " (not yet connected to a console)"
        )

    transport = Transport(
        url,
        on_connected=on_connected,
        on_disconnected=lambda: logging.getLogger("nxremote-client").warning("disconnected from host"),
        reconnect=not args.no_reconnect,
    )
    client = ForwarderClient(source, transport, sink, rate=args.rate)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, client.stop)
        except (NotImplementedError, ValueError):  # Windows / non-main thread
            pass

    try:
        source.open()
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"error opening input: {e}")
    print(f"forwarding '{source.device_name}' to {url.split('token=')[0].rstrip('?&')}  (Ctrl-C to stop)")
    try:
        await client.run()
    finally:
        source.close()


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.list:
        return _list_controllers()

    if not args.host:
        args.host = os.environ.get("NXREMOTE_HOST")
    if not args.host:
        print("error: no host given (positional HOST or $NXREMOTE_HOST)", file=sys.stderr)
        return 2

    source, sink, ctx = _make_source_and_sink(args)
    try:
        asyncio.run(_run(args, source, sink))
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sink.close()
        finally:
            if ctx is not None:
                ctx.__exit__(None, None, None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
