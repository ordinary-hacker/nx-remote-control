"""SDL2-backed gamepad source and rumble sink (Windows, macOS, Linux).

SDL2 reads virtually any game controller (Xbox, DualShock/DualSense, Switch Pro,
8BitDo, generic USB pads, ...) through one normalised "game controller" API,
and plays rumble back on pads that support it. The native SDL2 library is
provided by the ``pysdl2-dll`` wheel, so no system package is required on the
common desktop platforms.

Import this module only when SDL is actually wanted; it raises a friendly error
if the bindings or the native library are missing.
"""

from __future__ import annotations

import ctypes
import logging
from typing import Optional

from .mapping import (
    SDL_AXIS_LX,
    SDL_AXIS_LY,
    SDL_AXIS_RX,
    SDL_AXIS_RY,
    SDL_AXIS_TRIGGER_L,
    SDL_AXIS_TRIGGER_R,
    Mapping,
)
from .sources import InputSource, RumbleSink
from .state import ControllerState, RumbleState, clamp

log = logging.getLogger("nxremote_client.sdl")

_AXIS_MAX = 32767.0
_RUMBLE_MAX = 0xFFFF


class SDLUnavailable(RuntimeError):
    pass


def _import_sdl():
    try:
        import sdl2  # type: ignore
    except ImportError as e:
        raise SDLUnavailable(
            "SDL support needs the 'sdl' extra: pip install 'nxremote-client[sdl]'"
        ) from e
    return sdl2


class SDLContext:
    """Owns SDL init/quit and the game-controller mapping database."""

    def __init__(self) -> None:
        self.sdl = _import_sdl()

    def __enter__(self) -> "SDLContext":
        sdl = self.sdl
        # Let us keep reading the pad even when our window (if any) isn't focused.
        sdl.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
        if sdl.SDL_Init(sdl.SDL_INIT_GAMECONTROLLER | sdl.SDL_INIT_JOYSTICK) != 0:
            raise SDLUnavailable(f"SDL_Init failed: {sdl.SDL_GetError().decode(errors='replace')}")
        return self

    def __exit__(self, *exc) -> None:
        self.sdl.SDL_Quit()

    def list_controllers(self) -> list[tuple[int, str]]:
        sdl = self.sdl
        sdl.SDL_GameControllerUpdate()
        out = []
        for i in range(sdl.SDL_NumJoysticks()):
            if sdl.SDL_IsGameController(i):
                name = sdl.SDL_GameControllerNameForIndex(i)
                out.append((i, name.decode(errors="replace") if name else f"controller {i}"))
        return out


class SDLGamepadSource(InputSource):
    """Reads one SDL game controller and maps it to Switch inputs."""

    def __init__(self, ctx: SDLContext, index: int = 0, mapping: Optional[Mapping] = None,
                 deadzone: float = 0.08) -> None:
        self.ctx = ctx
        self.sdl = ctx.sdl
        self.index = index
        self.mapping = mapping or Mapping()
        self.deadzone = deadzone
        self._pad = None
        self._button_table = self.mapping.button_table()

    def open(self) -> None:
        controllers = self.ctx.list_controllers()
        if not controllers:
            raise SDLUnavailable("no game controller detected (plug one in, or pair it, and retry)")
        if self.index >= len(controllers):
            raise SDLUnavailable(f"controller #{self.index} not present; found {len(controllers)}")
        sdl_index = controllers[self.index][0]
        self._pad = self.sdl.SDL_GameControllerOpen(sdl_index)
        if not self._pad:
            raise SDLUnavailable(f"could not open controller: {self.sdl.SDL_GetError().decode(errors='replace')}")
        self.device_name = controllers[self.index][1]
        log.info("using controller: %s", self.device_name)

    def _axis(self, axis: int) -> float:
        v = self.sdl.SDL_GameControllerGetAxis(self._pad, axis) / _AXIS_MAX
        return 0.0 if abs(v) < self.deadzone else clamp(v)

    def poll(self) -> ControllerState:
        sdl = self.sdl
        sdl.SDL_GameControllerUpdate()
        buttons = set()
        for sdl_button, name in self._button_table.items():
            if sdl.SDL_GameControllerGetButton(self._pad, sdl_button):
                buttons.add(name)
        if self._axis_raw(SDL_AXIS_TRIGGER_L) >= self.mapping.trigger_threshold:
            buttons.add("ZL")
        if self._axis_raw(SDL_AXIS_TRIGGER_R) >= self.mapping.trigger_threshold:
            buttons.add("ZR")
        return ControllerState(
            buttons=frozenset(buttons),
            lx=self._axis(SDL_AXIS_LX),
            ly=-self._axis(SDL_AXIS_LY),  # SDL y grows downwards; host +y is up
            rx=self._axis(SDL_AXIS_RX),
            ry=-self._axis(SDL_AXIS_RY),
        )

    def _axis_raw(self, axis: int) -> float:
        return self.sdl.SDL_GameControllerGetAxis(self._pad, axis) / _AXIS_MAX

    def close(self) -> None:
        if self._pad:
            self.sdl.SDL_GameControllerClose(self._pad)
            self._pad = None


class SDLRumbleSink(RumbleSink):
    """Plays rumble on the same SDL controller the source reads."""

    def __init__(self, source: SDLGamepadSource, duration_ms: int = 300) -> None:
        self.source = source
        self.sdl = source.sdl
        self.duration_ms = duration_ms
        self._supported = True
        self._last = (-1, -1)

    def rumble(self, state: RumbleState) -> None:
        pad = self.source._pad
        if not pad or not self._supported:
            return
        low = int(clamp(state.left, 0, 1) * _RUMBLE_MAX)
        high = int(clamp(state.right, 0, 1) * _RUMBLE_MAX)
        if (low, high) == self._last:
            return
        self._last = (low, high)
        # 0 stops; otherwise keep playing a bit longer than the event interval so
        # sustained rumble doesn't gap between updates.
        duration = 0 if (low == 0 and high == 0) else self.duration_ms
        rc = self.sdl.SDL_GameControllerRumble(pad, low, high, duration)
        if rc != 0:
            self._supported = False
            log.info("this controller reports no rumble support; disabling playback")
