"""Analog stick calibration and the 12-bit x/y packing used by the protocol."""

from __future__ import annotations

from dataclasses import dataclass

from .state import Stick


@dataclass(frozen=True)
class StickCalibration:
    center_x: int
    center_y: int
    above_x: int  # travel from centre to the +x extreme
    above_y: int  # travel from centre to the +y extreme
    below_x: int  # travel from centre to the -x extreme
    below_y: int  # travel from centre to the -y extreme

    def to_raw(self, stick: Stick) -> tuple[int, int]:
        s = stick.clamped()
        x = self.center_x + s.x * (self.above_x if s.x >= 0 else self.below_x)
        y = self.center_y + s.y * (self.above_y if s.y >= 0 else self.below_y)
        return int(round(x)) & 0xFFF, int(round(y)) & 0xFFF


# Values taken from a real Pro Controller's factory calibration.
LEFT_CAL = StickCalibration(2159, 1916, 1466, 1583, 1517, 1465)
RIGHT_CAL = StickCalibration(2070, 2013, 1414, 1510, 1522, 1531)


def pack12(x: int, y: int) -> bytes:
    """Pack two 12-bit values into 3 little-endian bytes."""
    x &= 0xFFF
    y &= 0xFFF
    return bytes((x & 0xFF, ((y & 0xF) << 4) | (x >> 8), y >> 4))


def unpack12(data: bytes) -> tuple[int, int]:
    x = data[0] | ((data[1] & 0x0F) << 8)
    y = (data[1] >> 4) | (data[2] << 4)
    return x, y


def encode_stick(cal: StickCalibration, stick: Stick) -> bytes:
    return pack12(*cal.to_raw(stick))


def left_factory_calibration() -> bytes:
    """9 bytes at SPI 0x603D: max above centre, centre, max below centre."""
    c = LEFT_CAL
    return pack12(c.above_x, c.above_y) + pack12(c.center_x, c.center_y) + pack12(c.below_x, c.below_y)


def right_factory_calibration() -> bytes:
    """9 bytes at SPI 0x6046: centre, max below centre, max above centre."""
    c = RIGHT_CAL
    return pack12(c.center_x, c.center_y) + pack12(c.below_x, c.below_y) + pack12(c.above_x, c.above_y)


def stick_to_u16(stick: Stick) -> tuple[int, int]:
    """Axis values for the simple (0x3F) report: 0..65535 with 0x8000 centre."""
    s = stick.clamped()
    return (
        min(0xFFFF, int(round(0x8000 + s.x * 0x7FFF))),
        min(0xFFFF, int(round(0x8000 + s.y * 0x7FFF))),
    )
