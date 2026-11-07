"""The runner: poll a local source, forward changes to the host, play rumble back."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .sources import InputSource, NullSink, RumbleSink
from .state import NEUTRAL, ControllerState, RumbleState
from .transport import Transport

log = logging.getLogger("nxremote_client.runner")


class ForwarderClient:
    """Forwards a local controller to an nxremote host and plays rumble back.

    The source is polled at ``rate`` Hz. A new state is sent only when it
    differs from the last one sent, with a keep-alive resend every
    ``keepalive`` seconds so a dropped packet can't leave the host stuck on a
    stale input. Rumble events from the host are handed to ``sink`` immediately.
    """

    def __init__(
        self,
        source: InputSource,
        transport: Transport,
        sink: Optional[RumbleSink] = None,
        *,
        rate: float = 120.0,
        keepalive: float = 1.0,
    ) -> None:
        self.source = source
        self.transport = transport
        self.sink = sink or NullSink()
        self.period = 1.0 / rate
        self.keepalive = keepalive
        self._stop = asyncio.Event()
        self._pending_rumble: Optional[RumbleState] = None
        # The transport dispatches rumble on the loop thread; we just stash the
        # latest and apply it in the main loop so the sink is touched from one place.
        transport.on_rumble = self._on_rumble

    def _on_rumble(self, rumble: RumbleState) -> None:
        self._pending_rumble = rumble

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        transport_task = asyncio.create_task(self.transport.run(), name="transport")
        try:
            await self._poll_loop()
        finally:
            self.transport.stop()
            transport_task.cancel()
            try:
                await transport_task
            except asyncio.CancelledError:
                pass
            try:
                self.sink.rumble(RumbleState())  # stop any lingering rumble
            except Exception:  # noqa: BLE001
                pass

    async def _poll_loop(self) -> None:
        loop = asyncio.get_running_loop()
        last_sent: Optional[ControllerState] = None
        last_sent_at = 0.0
        was_connected = False

        while not self._stop.is_set():
            tick = loop.time()

            # Polling a hardware source can block (SDL C calls); keep the loop free.
            try:
                state = (await loop.run_in_executor(None, self.source.poll)).normalized()
            except Exception:  # noqa: BLE001 - a flaky poll must not kill the client
                log.exception("input poll failed")
                state = NEUTRAL

            if self.transport.connected:
                if not was_connected:
                    was_connected = True
                    last_sent = None  # force a full resend on (re)connect
                now = time.monotonic()
                if state != last_sent or (now - last_sent_at) >= self.keepalive:
                    await self.transport.send_state(state)
                    last_sent = state
                    last_sent_at = now
            else:
                was_connected = False
                last_sent = None

            if self._pending_rumble is not None:
                rumble, self._pending_rumble = self._pending_rumble, None
                try:
                    self.sink.rumble(rumble)
                except Exception:  # noqa: BLE001
                    log.exception("rumble playback failed")

            elapsed = loop.time() - tick
            await asyncio.sleep(max(0.0, self.period - elapsed))
