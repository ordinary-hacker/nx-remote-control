"""The controller state the client produces, plus the Switch button vocabulary.

Kept free of any I/O or SDL import so it can be used (and tested) anywhere. The
button names and stick convention match the nxremote host exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Switch button names understood by the host (see nxremote.hid.state.BUTTONS).
SWITCH_BUTTONS = (
    "A", "B", "X", "Y",
    "L", "R", "ZL", "ZR",
    "MINUS", "PLUS", "HOME", "CAPTURE",
    "L_STICK", "R_STICK",
    "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
)


def clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


@dataclass(frozen=True)
class ControllerState:
    """A snapshot of the local controller, in the host's coordinate system.

    Sticks: x in [-1, 1] (+right), y in [-1, 1] (+up).
    """

    buttons: frozenset[str] = frozenset()
    lx: float = 0.0
    ly: float = 0.0
    rx: float = 0.0
    ry: float = 0.0

    def normalized(self) -> "ControllerState":
        return ControllerState(
            frozenset(self.buttons),
            clamp(self.lx), clamp(self.ly), clamp(self.rx), clamp(self.ry),
        )

    def to_input_message(self) -> dict:
        """The ``{"type":"input", ...}`` WebSocket payload for the host."""
        return {
            "type": "input",
            "buttons": sorted(self.buttons),
            "left_stick": {"x": round(self.lx, 4), "y": round(self.ly, 4)},
            "right_stick": {"x": round(self.rx, 4), "y": round(self.ry, 4)},
        }


NEUTRAL = ControllerState()


@dataclass
class RumbleState:
    """Latest rumble intensities received from the host (0..1 per actuator)."""

    left: float = 0.0
    right: float = 0.0

    @property
    def active(self) -> bool:
        return self.left > 0.0 or self.right > 0.0
