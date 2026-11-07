"""Platform-independent transport to an nxremote host over WebSocket.

Depends only on ``websockets`` and the stdlib, so it runs anywhere CPython
runs (desktop, Termux on Android, a Raspberry Pi, ...). Knows nothing about
SDL or any particular input device: you push :class:`ControllerState` snapshots
in and register callbacks for rumble/state events coming back.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import websockets

from .state import NEUTRAL, ControllerState, RumbleState

log = logging.getLogger("nxremote_client.transport")

RumbleCallback = Callable[[RumbleState], None]
EventCallback = Callable[[dict], None]


def build_ws_url(host: str, token: str = "", release_on_close: bool = True) -> str:
    """Normalise a host given as ``host[:port]``, ``http(s)://...`` or ``ws(s)://...``.

    Appends ``/v1/ws`` and the query string the host expects.
    """
    raw = host.strip()
    if "://" not in raw:
        raw = "ws://" + raw
    parts = urlsplit(raw)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    if scheme not in ("ws", "wss"):
        raise ValueError(f"unsupported scheme in {host!r}")
    netloc = parts.netloc or parts.path  # "1.2.3.4:8000" with no scheme lands in path
    path = parts.path if parts.netloc else ""
    if not netloc:
        raise ValueError(f"no host in {host!r}")
    if ":" not in netloc.split("@")[-1]:
        netloc = f"{netloc}:8000"
    path = (path.rstrip("/") + "/v1/ws") if path.strip("/") not in ("", "v1", "v1/ws") else "/v1/ws"
    query = []
    if token:
        query.append("token=" + quote(token, safe=""))
    if not release_on_close:
        query.append("release_on_close=false")
    return urlunsplit((scheme, netloc, path, "&".join(query), ""))


class HostConnection:
    """One live WebSocket session with the host."""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._next_id = 1
        self.hello: dict = {}

    async def send_state(self, state: ControllerState) -> None:
        await self._ws.send(_dumps(state.normalized().to_input_message()))

    async def send_message(self, message: dict) -> None:
        await self._ws.send(_dumps(message))

    async def release_all(self) -> None:
        await self._ws.send(_dumps({"type": "release_all"}))


def _dumps(obj: dict) -> str:
    import json

    return json.dumps(obj, separators=(",", ":"))


class Transport:
    """Manages the connection lifecycle: connect, (re)connect, read loop.

    Callbacks fire on the asyncio loop thread. ``on_rumble`` gets a
    :class:`RumbleState`; ``on_event`` gets every raw event dict (state, mode,
    player_lights, ...); ``on_connected`` gets the host's ``hello`` payload.
    """

    def __init__(
        self,
        url: str,
        *,
        on_rumble: Optional[RumbleCallback] = None,
        on_event: Optional[EventCallback] = None,
        on_connected: Optional[Callable[[dict], None]] = None,
        on_disconnected: Optional[Callable[[], None]] = None,
        reconnect: bool = True,
        reconnect_delay: float = 2.0,
    ) -> None:
        self.url = url
        self.on_rumble = on_rumble
        self.on_event = on_event
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.reconnect = reconnect
        self.reconnect_delay = reconnect_delay

        self._conn: Optional[HostConnection] = None
        self._ws = None
        self._conn_ready = asyncio.Event()
        self._stop = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def wait_connected(self, timeout: Optional[float] = None) -> None:
        await asyncio.wait_for(self._conn_ready.wait(), timeout)

    async def send_state(self, state: ControllerState) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            await conn.send_state(state)
        except websockets.ConnectionClosed:
            pass

    async def send_message(self, message: dict) -> None:
        conn = self._conn
        if conn is not None:
            try:
                await conn.send_message(message)
            except websockets.ConnectionClosed:
                pass

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            # Close the live socket so the read loop exits promptly instead of
            # blocking in recv() until the next ping timeout.
            try:
                asyncio.create_task(ws.close())
            except RuntimeError:
                pass

    async def run(self) -> None:
        """Connect and service the session until stopped. Reconnects on drop."""
        redacted = self.url.split("token=")[0].rstrip("?&")
        while not self._stop.is_set():
            try:
                log.info("connecting to %s", redacted)
                async with websockets.connect(
                    self.url, ping_interval=20, ping_timeout=20, max_queue=32, open_timeout=10
                ) as ws:
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except (OSError, websockets.WebSocketException) as e:
                log.warning("connection failed: %s", e)
            except Exception:  # noqa: BLE001 - keep the loop alive, log for debugging
                log.exception("unexpected transport error")
            finally:
                self._ws = None
                if self._conn is not None:
                    self._conn = None
                    self._conn_ready.clear()
                    if self.on_disconnected:
                        self.on_disconnected()
            if not self.reconnect or self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), self.reconnect_delay)
            except asyncio.TimeoutError:
                pass

    async def _session(self, ws) -> None:
        import json

        self._ws = ws
        conn = HostConnection(ws)
        # The first frame is always the host's "hello" with full status.
        first = json.loads(await ws.recv())
        conn.hello = first
        self._conn = conn
        self._conn_ready.set()
        log.info("connected (controller state: %s)", first.get("controller", {}).get("state"))
        if self.on_connected:
            self.on_connected(first)
        self._dispatch(first)
        async for raw in ws:
            if self._stop.is_set():
                break
            try:
                self._dispatch(json.loads(raw))
            except (ValueError, TypeError):
                log.debug("ignoring malformed frame")

    def _dispatch(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "rumble" and self.on_rumble:
            self.on_rumble(RumbleState(
                left=float(event.get("left", {}).get("intensity", 0.0)),
                right=float(event.get("right", {}).get("intensity", 0.0)),
            ))
        if self.on_event:
            self.on_event(event)
