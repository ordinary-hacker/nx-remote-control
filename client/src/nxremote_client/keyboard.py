"""A keyboard input source, as a fallback when no gamepad is available.

Uses SDL's keyboard state if an SDL context is handy (works cross-platform and
needs a focused SDL window), but by default provides a dependency-free terminal
reader for Linux/macOS. It is intentionally simple: it is a convenience and a
test aid, not a replacement for a real controller.

Default WASD + J/K/L/I layout::

    movement : W A S D        -> left stick
    camera   : arrow keys     -> right stick
    faces    : J K L I        -> B A R-side ... (see KEYMAP)
    triggers : Q E            -> ZL ZR
    shoulders: U O            -> L R
    dpad     : T F G H        -> up left down right
    system   : Enter/Backspace/Esc -> PLUS/MINUS/HOME, Tab -> CAPTURE
"""

from __future__ import annotations

import sys
from typing import Optional

from .sources import InputSource
from .state import ControllerState

KEYMAP = {
    "j": "B", "k": "A", "l": "X", "i": "Y",
    "u": "L", "o": "R", "q": "ZL", "e": "ZR",
    "t": "DPAD_UP", "f": "DPAD_LEFT", "g": "DPAD_DOWN", "h": "DPAD_RIGHT",
    "\r": "PLUS", "\n": "PLUS", "\x7f": "MINUS", "\x1b": "HOME", "\t": "CAPTURE",
    "z": "L_STICK", "x": "R_STICK",
}
STICK = {"w": ("ly", 1), "s": ("ly", -1), "a": ("lx", -1), "d": ("lx", 1)}


class KeyboardSource(InputSource):
    """Non-blocking terminal keyboard reader (POSIX).

    Keys act as momentary presses: a key is reported held for ``hold_polls``
    polls after it is pressed, since a terminal cannot report key-release.
    """

    def __init__(self, hold_polls: int = 6) -> None:
        self.device_name = "keyboard (terminal)"
        self.hold_polls = hold_polls
        self._held: dict[str, int] = {}
        self._fd: Optional[int] = None
        self._old = None

    def open(self) -> None:
        if not sys.stdin.isatty():
            raise RuntimeError("keyboard source needs an interactive terminal")
        try:
            import termios  # noqa: F401
            import tty  # noqa: F401
        except ImportError as e:  # Windows
            raise RuntimeError("terminal keyboard source is POSIX-only; use a gamepad on Windows") from e
        import termios
        import tty

        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)

    def _read_keys(self) -> list[str]:
        import select

        keys = []
        while select.select([sys.stdin], [], [], 0)[0]:
            ch = sys.stdin.read(1)
            if not ch:
                break
            keys.append(ch.lower())
        return keys

    def poll(self) -> ControllerState:
        for ch in self._read_keys():
            self._held[ch] = self.hold_polls
        buttons = set()
        lx = ly = rx = ry = 0.0
        for ch, remaining in list(self._held.items()):
            if ch in KEYMAP:
                buttons.add(KEYMAP[ch])
            if ch in STICK:
                attr, sign = STICK[ch]
                if attr == "lx":
                    lx += sign
                else:
                    ly += sign
            self._held[ch] = remaining - 1
            if self._held[ch] <= 0:
                del self._held[ch]
        return ControllerState(frozenset(buttons), lx, ly, rx, ry).normalized()

    def close(self) -> None:
        if self._fd is not None and self._old is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            self._fd = None
