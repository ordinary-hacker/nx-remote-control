"""Six-axis sensor sample encoding."""

from __future__ import annotations

import struct

from .state import Imu, clamp

ACCEL_LSB_PER_G = 4096.0
GYRO_LSB_PER_DPS = 0x343B / 936.0  # ~14.285, matches the calibration in spi.py


def _s16(v: float) -> int:
    return int(clamp(round(v), -32768, 32767))


def encode_sample(imu: Imu) -> bytes:
    """12 bytes: accel x,y,z then gyro x,y,z as little-endian int16."""
    return struct.pack(
        "<6h",
        *(_s16(a * ACCEL_LSB_PER_G) for a in imu.accel),
        *(_s16(g * GYRO_LSB_PER_DPS) for g in imu.gyro),
    )


def encode_report_block(imu: Imu) -> bytes:
    """36 bytes: three samples (the console expects 3 per 15 ms report)."""
    return encode_sample(imu) * 3
