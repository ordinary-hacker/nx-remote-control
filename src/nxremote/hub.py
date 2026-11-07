"""Thread-safe holder of the desired controller state.

Two layers are merged to produce what the console sees:

* the *direct* state, set by API clients (``PUT``/``PATCH /v1/input`` ...)
* *overlays*, temporary partial states applied by macros/taps

Buttons are OR-ed together; sticks and IMU come from the newest overlay that
specifies them, else from the direct state.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from .hid.state import NEUTRAL, Battery, ControllerState, Imu, Stick


@dataclass(frozen=True)
class Overlay:
    buttons: frozenset[str] = frozenset()
    left_stick: Optional[Stick] = None
    right_stick: Optional[Stick] = None
    imu: Optional[Imu] = None


class InputHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._base = NEUTRAL
        self._overlays: dict[int, Overlay] = {}
        self._ids = itertools.count(1)
        self._snapshot = NEUTRAL
        self._wakers: list[Callable[[], None]] = []
        self.revision = 0

    # -- observers ---------------------------------------------------------

    def add_waker(self, fn: Callable[[], None]) -> None:
        self._wakers.append(fn)

    def remove_waker(self, fn: Callable[[], None]) -> None:
        if fn in self._wakers:
            self._wakers.remove(fn)

    def _changed(self) -> None:
        merged = self._merge()
        if merged != self._snapshot:
            self._snapshot = merged
            self.revision += 1
            wake = True
        else:
            wake = False
        if wake:
            for fn in list(self._wakers):
                fn()

    def _merge(self) -> ControllerState:
        s = self._base
        if not self._overlays:
            return s
        buttons = set(s.buttons)
        left, right, imu = s.left_stick, s.right_stick, s.imu
        for ov in self._overlays.values():  # insertion order: newest wins
            buttons |= ov.buttons
            if ov.left_stick is not None:
                left = ov.left_stick
            if ov.right_stick is not None:
                right = ov.right_stick
            if ov.imu is not None:
                imu = ov.imu
        return ControllerState(frozenset(buttons), left, right, imu, s.battery)

    # -- reads -------------------------------------------------------------

    def snapshot(self) -> ControllerState:
        """What the console currently sees (direct state + overlays)."""
        return self._snapshot

    def direct(self) -> ControllerState:
        with self._lock:
            return self._base

    # -- direct state ------------------------------------------------------

    def set_direct(self, state: ControllerState) -> None:
        with self._lock:
            self._base = state
            self._changed()

    def update_direct(self, **changes) -> ControllerState:
        with self._lock:
            self._base = self._base.with_(**changes)
            self._changed()
            return self._base

    def modify(self, fn: Callable[[ControllerState], ControllerState]) -> ControllerState:
        """Atomically transform the direct state."""
        with self._lock:
            self._base = fn(self._base)
            self._changed()
            return self._base

    def press(self, buttons: frozenset[str]) -> None:
        with self._lock:
            self._base = self._base.with_(buttons=self._base.buttons | buttons)
            self._changed()

    def release(self, buttons: frozenset[str]) -> None:
        with self._lock:
            self._base = self._base.with_(buttons=self._base.buttons - buttons)
            self._changed()

    def set_battery(self, battery: Battery) -> None:
        self.update_direct(battery=battery)

    def reset(self) -> None:
        """Release everything: buttons, sticks, motion (keeps battery)."""
        with self._lock:
            self._base = NEUTRAL.with_(battery=self._base.battery)
            self._overlays.clear()
            self._changed()

    # -- overlays ----------------------------------------------------------

    def push_overlay(self, overlay: Overlay) -> int:
        with self._lock:
            oid = next(self._ids)
            self._overlays[oid] = overlay
            self._changed()
            return oid

    def replace_overlay(self, oid: int, overlay: Overlay) -> None:
        with self._lock:
            if oid in self._overlays:
                self._overlays[oid] = overlay
                self._changed()

    def pop_overlay(self, oid: int) -> None:
        with self._lock:
            if self._overlays.pop(oid, None) is not None:
                self._changed()
