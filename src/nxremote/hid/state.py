"""Controller input state model and the button name table."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable

# name -> (byte index within the 3 button bytes, bit mask)
# Byte 0 = "right" byte, 1 = "shared" byte, 2 = "left" byte of the 0x30 report.
BUTTONS: dict[str, tuple[int, int]] = {
    "Y": (0, 0x01),
    "X": (0, 0x02),
    "B": (0, 0x04),
    "A": (0, 0x08),
    "R": (0, 0x40),
    "ZR": (0, 0x80),
    "MINUS": (1, 0x01),
    "PLUS": (1, 0x02),
    "R_STICK": (1, 0x04),
    "L_STICK": (1, 0x08),
    "HOME": (1, 0x10),
    "CAPTURE": (1, 0x20),
    "DPAD_DOWN": (2, 0x01),
    "DPAD_UP": (2, 0x02),
    "DPAD_RIGHT": (2, 0x04),
    "DPAD_LEFT": (2, 0x08),
    "L": (2, 0x40),
    "ZL": (2, 0x80),
}

BUTTON_NAMES = tuple(BUTTONS)

_ALIASES = {
    "UP": "DPAD_UP",
    "DOWN": "DPAD_DOWN",
    "LEFT": "DPAD_LEFT",
    "RIGHT": "DPAD_RIGHT",
    "LS": "L_STICK",
    "RS": "R_STICK",
    "LSTICK": "L_STICK",
    "RSTICK": "R_STICK",
    "L_STICK_PRESS": "L_STICK",
    "R_STICK_PRESS": "R_STICK",
    "L3": "L_STICK",
    "R3": "R_STICK",
    "SELECT": "MINUS",
    "START": "PLUS",
    "SCREENSHOT": "CAPTURE",
}


class InvalidButton(ValueError):
    pass


def normalize_button(name: str) -> str:
    key = str(name).strip().upper().replace("-", "_").replace(" ", "_")
    key = _ALIASES.get(key, key)
    if key not in BUTTONS:
        raise InvalidButton(
            f"unknown button {name!r}; valid: {', '.join(BUTTON_NAMES)}"
        )
    return key


def normalize_buttons(names: Iterable[str]) -> frozenset[str]:
    return frozenset(normalize_button(n) for n in names)


def clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


@dataclass(frozen=True)
class Stick:
    """Stick position, each axis in [-1.0, 1.0]. +x is right, +y is up."""

    x: float = 0.0
    y: float = 0.0

    def clamped(self) -> "Stick":
        return Stick(clamp(float(self.x), -1.0, 1.0), clamp(float(self.y), -1.0, 1.0))


@dataclass(frozen=True)
class Imu:
    """Six-axis sensor reading. Accelerometer in g, gyroscope in degrees/s.

    The default is a controller lying flat and still (1 g on +Z).
    """

    accel: tuple[float, float, float] = (0.0, 0.0, 1.0)
    gyro: tuple[float, float, float] = (0.0, 0.0, 0.0)


# Battery levels as reported in the upper nibble of byte 2 of every report.
BATTERY_LEVELS = {"empty": 0, "critical": 1, "low": 2, "medium": 3, "full": 4}


@dataclass(frozen=True)
class Battery:
    level: str = "full"
    charging: bool = False

    def nibble(self) -> int:
        return (BATTERY_LEVELS[self.level] << 1 | (1 if self.charging else 0)) << 4


@dataclass(frozen=True)
class ControllerState:
    """An immutable snapshot of everything the host can observe."""

    buttons: frozenset[str] = frozenset()
    left_stick: Stick = field(default_factory=Stick)
    right_stick: Stick = field(default_factory=Stick)
    imu: Imu = field(default_factory=Imu)
    battery: Battery = field(default_factory=Battery)

    def with_(self, **kw) -> "ControllerState":
        return replace(self, **kw)

    def to_dict(self) -> dict:
        return {
            "buttons": sorted(self.buttons),
            "left_stick": {"x": self.left_stick.x, "y": self.left_stick.y},
            "right_stick": {"x": self.right_stick.x, "y": self.right_stick.y},
            "imu": {"accel": list(self.imu.accel), "gyro": list(self.imu.gyro)},
            "battery": {"level": self.battery.level, "charging": self.battery.charging},
        }


NEUTRAL = ControllerState()
