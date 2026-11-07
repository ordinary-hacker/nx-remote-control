"""Emulated SPI flash of a Pro Controller (factory data the console reads)."""

from __future__ import annotations

import struct

from .sticks import left_factory_calibration, right_factory_calibration

FLASH_SIZE = 0x10000

# Stick device parameters (dead zone, range ratio...). Pro Controller dead zone: 15%.
_STICK_PARAMS = bytes(
    [0x0F, 0x30, 0x61, 0x96, 0x30, 0xF3, 0xD4, 0x14, 0x54, 0x41, 0x15, 0x54, 0xC7, 0x79, 0x9C, 0x33, 0x36, 0x63]
)

# IMU calibration (SPI 0x6020, 24 bytes): offsets are zeroed so the raw readings
# are simply 4096 LSB/g and ~14.285 LSB/(deg/s); see hid/imu.py.
ACCEL_ORIGIN = (0, 0, 0)
ACCEL_SENS = (0x4000, 0x4000, 0x4000)
GYRO_ORIGIN = (0, 0, 0)
GYRO_SENS = (0x343B, 0x343B, 0x343B)


def imu_calibration() -> bytes:
    return struct.pack("<12h", *ACCEL_ORIGIN, *ACCEL_SENS, *GYRO_ORIGIN, *GYRO_SENS)


def build_flash(body_colour: bytes, button_colour: bytes, grip_colours: bytes | None = None) -> bytearray:
    f = bytearray(b"\xff" * FLASH_SIZE)

    # Serial number: all 0xFF means "none".
    # Colours (0x6050): body, buttons, left grip, right grip.
    grips = grip_colours if grip_colours is not None else b"\xff" * 6
    f[0x6050:0x6050 + 12] = body_colour + button_colour + grips

    # Factory IMU calibration + six-axis "factory parameters" blob.
    f[0x6020:0x6020 + 24] = imu_calibration()
    f[0x6080:0x6080 + 6] = bytes([0x50, 0xFD, 0x00, 0x00, 0xC6, 0x0F])
    f[0x6086:0x6086 + 18] = _STICK_PARAMS
    f[0x6098:0x6098 + 18] = _STICK_PARAMS

    # Factory stick calibration (0x603D left, 0x6046 right), then colours again at 0x6050.
    f[0x603D:0x603D + 9] = left_factory_calibration()
    f[0x6046:0x6046 + 9] = right_factory_calibration()
    # Magic bytes of the "user calibration" areas stay 0xFF => no user calibration.
    return f
