"""The controller core: connection lifecycle + the real-time report loop.

Runs in its own thread so API/event-loop jitter can never delay HID reports.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

from .hid import rumble as rumble_mod
from .hid.protocol import (
    DEFAULT_BODY_COLOUR,
    DEFAULT_BUTTON_COLOUR,
    ProControllerProtocol,
)
from .hub import InputHub
from .store import PairingStore
from .transport import Connector, Link, LinkClosed, Stopped, wait_readable

log = logging.getLogger(__name__)

Listener = Callable[[str, dict], None]

MIN_REPORT_GAP = 0.006  # s; caps the rate of change-driven reports (~160 Hz)
STREAM_INTERVAL = 0.015  # s; 66 Hz like a real controller when motion is on
KEEPALIVE_INTERVAL = 1.0  # s; idle keepalive when nothing changes
PAIRING_FIRST_INTERVAL = 1.0  # s; before the console has said anything
PAIRING_INTERVAL = 1 / 15  # s; during the console's initial configuration
HANDSHAKE_IDLE_TIMEOUT = 2.0  # s; consider connected if the console goes quiet
RECONNECT_ATTEMPTS = 3


class ControllerService:
    def __init__(
        self,
        connector: Connector,
        hub: InputHub,
        store: PairingStore,
        *,
        body_colour: bytes = DEFAULT_BODY_COLOUR,
        button_colour: bytes = DEFAULT_BUTTON_COLOUR,
    ) -> None:
        self.connector = connector
        self.hub = hub
        self.store = store
        self.body_colour = body_colour
        self.button_colour = button_colour

        self._listeners: list[Listener] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        self.hub.add_waker(self._wake)

        self.state = "stopped"
        self.error: Optional[str] = None
        self.peer: Optional[str] = None
        self.connected_since: Optional[float] = None
        self.connections = 0
        self.protocol: Optional[ProControllerProtocol] = None
        self.last_rumble: Optional[dict] = None
        self.started_at = time.time()

    # -- listeners / status ---------------------------------------------------

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Listener) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _emit(self, name: str, payload: dict) -> None:
        if name == "rumble":
            payload = {**payload, "time": time.time()}
            self.last_rumble = payload
        for fn in list(self._listeners):
            try:
                fn(name, payload)
            except Exception:  # a broken listener must never kill the core
                log.exception("listener failed")

    def _set_state(self, state: str, error: Optional[str] = None) -> None:
        if state == self.state and error == self.error:
            return
        self.state = state
        self.error = error
        log.info("state: %s%s", state, f" ({error})" if error else "")
        self._emit("state", self.status())

    def status(self) -> dict:
        live = self.state in ("pairing", "connected")
        p = self.protocol if live else None
        return {
            "state": self.state,
            "connected": self.state == "connected",
            "peer": self.peer if live else None,
            "error": self.error,
            "backend": type(self.connector).__name__,
            "adapter_address": self.connector.address,
            "known_consoles": self.store.load(),
            "connections": self.connections,
            "connected_since": self.connected_since if self.state == "connected" else None,
            "mode": p.mode_name if p else None,
            "imu_enabled": p.imu_enabled if p else False,
            "vibration_enabled": p.vibration_enabled if p else False,
            "player_lights": p.player_lights.to_dict() if p and p.player_lights_set else None,
            "home_light": p.home_light.hex() if p and p.home_light else None,
            "last_rumble": self.last_rumble,
        }

    # -- lifecycle -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, reconnect: bool = True, address: Optional[str] = None) -> bool:
        """Start connecting. Returns False if already running."""
        with self._lock:
            if self.running:
                return False
            self._stop.clear()
            self.error = None
            self._thread = threading.Thread(
                target=self._main, args=(reconnect, address), name="nxremote-core", daemon=True
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            t = self._thread
            self._stop.set()
            self._wake()
        if t is not None:
            t.join(timeout)
        self._thread = None

    def _wake(self) -> None:
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass

    def _drain_wake(self) -> None:
        try:
            while os.read(self._wake_r, 256):
                pass
        except OSError:
            pass

    # -- main thread --------------------------------------------------------------

    def _main(self, reconnect: bool, address: Optional[str]) -> None:
        try:
            self._set_state("initializing")
            self.connector.prepare()
            targets = [address.upper()] if address else (self.store.load() if reconnect else [])
            while not self._stop.is_set():
                try:
                    link = self._acquire_link(targets)
                except Stopped:
                    break
                except OSError as e:
                    log.warning("connection setup failed: %s", e)
                    self._set_state("error", str(e))
                    if self._stop.wait(2.0):
                        break
                    continue
                try:
                    self._run_link(link)
                finally:
                    link.close()
                    self.connected_since = None
                    self.protocol = None
                if self._stop.is_set():
                    break
                self._set_state("disconnected")
                # Prefer reconnecting to the console that just dropped us.
                targets = ([self.peer] if self.peer else []) + [t for t in targets if t != self.peer]
                self._stop.wait(0.5)
        except Exception as e:
            log.exception("controller core crashed")
            self._set_state("error", f"{type(e).__name__}: {e}")
        finally:
            try:
                self.connector.cleanup()
            except Exception:
                log.exception("cleanup failed")
            self.hub.reset()
            if self.state != "error":
                self._set_state("stopped")

    def _acquire_link(self, targets: list[str]) -> Link:
        for _ in range(RECONNECT_ATTEMPTS if targets else 0):
            for addr in targets:
                if self._stop.is_set():
                    raise Stopped()
                self._set_state("reconnecting")
                try:
                    link = self.connector.connect(addr, self._stop)
                    link.peer = link.peer or addr
                    return link
                except OSError as e:
                    log.debug("reconnect to %s failed: %s", addr, e)
            if self._stop.wait(1.0):
                raise Stopped()
        self._set_state("waiting")
        return self.connector.listen(self._stop)

    # -- per-connection loop ------------------------------------------------------

    def _run_link(self, link: Link) -> None:
        self.peer = link.peer or None
        self.connections += 1
        proto = ProControllerProtocol(
            self.connector.address,
            body_colour=self.body_colour,
            button_colour=self.button_colour,
            on_event=self._emit,
        )
        self.protocol = proto
        self._set_state("pairing")
        self._drain_wake()

        started = time.monotonic()
        last_sent_at = 0.0
        last_sent = b""
        connected = False
        received_any = False

        try:
            link.send(proto.build_input_report(self.hub.snapshot()))
            last_sent_at = time.monotonic()

            while not self._stop.is_set():
                now = time.monotonic()
                quiet = received_any and proto.mode != 0x3F and now - proto.last_subcommand_at > HANDSHAKE_IDLE_TIMEOUT
                if not connected and (proto.handshake_complete or quiet):
                    connected = True
                    self.connected_since = time.time()
                    if self.peer:
                        self.store.remember(self.peer)
                    self._set_state("connected")

                # Work out when we next need to send.
                if not connected:
                    interval = PAIRING_INTERVAL if received_any else PAIRING_FIRST_INTERVAL
                else:
                    interval = STREAM_INTERVAL if proto.imu_enabled else KEEPALIVE_INTERVAL
                timeout = max(0.0, last_sent_at + interval - now)
                if connected and proto.build_input_report(self.hub.snapshot())[3:] != last_sent[3:]:
                    timeout = max(0.0, last_sent_at + MIN_REPORT_GAP - now)
                if timeout > 0:
                    wait_readable([*link.fileno_list(), self._wake_r], min(timeout, 0.25))
                    self._drain_wake()

                # Process everything the console sent, replying immediately.
                while True:
                    data = link.recv()
                    if data is None:
                        break
                    received_any = True
                    reply = proto.handle_output(data, self.hub.snapshot())
                    if reply:
                        link.send(reply)
                        last_sent_at = time.monotonic()
                        last_sent = reply
                if proto.disconnect_requested:
                    time.sleep(0.05)
                    return

                now = time.monotonic()
                report = proto.build_input_report(self.hub.snapshot())
                changed = connected and report[3:] != last_sent[3:]
                if (changed and now - last_sent_at >= MIN_REPORT_GAP) or now - last_sent_at >= interval:
                    link.send(report)
                    last_sent = report
                    last_sent_at = now
        except LinkClosed:
            log.info("console disconnected after %.1fs", time.monotonic() - started)
        finally:
            # Tell listeners the actuators are no longer driven.
            if self._rumble_active():
                neutral = rumble_mod.decode(rumble_mod.NEUTRAL_RUMBLE).to_dict()
                self._emit("rumble", {"left": neutral, "right": neutral, "reason": "disconnected"})

    def _rumble_active(self) -> bool:
        lr = self.last_rumble or {}
        return bool(lr.get("left", {}).get("active") or lr.get("right", {}).get("active"))
