"""Compact binary wire format for the realtime input channel (opt-in).

A JSON ``{"type":"input", ...}`` frame is ~100 bytes and must be parsed; this is
a fixed 12-byte frame a latency-sensitive client can send instead. JSON stays the
default and fully supported — a client simply sends a WebSocket *binary* frame in
this layout and the host decodes it to the same input path.

Layout (little-endian, 12 bytes)::

    offset  size  field
    0       1     magic/version = 0xB1
    1       3     button bitmask, bit i = BUTTON_NAMES[i] (18 buttons -> 18 bits)
    4       2     left stick x   int16, -32767..32767  == -1.0..1.0
    6       2     left stick y   int16
    8       2     right stick x  int16
    10      2     right stick y  int16

Sticks use the host convention (+x right, +y up). This frame carries buttons and
sticks only — the common realtime case; IMU/battery/taps/macros still go as JSON.
"""

from __future__ import annotations

import struct

from .state import BUTTON_NAMES, ControllerState, Stick

MAGIC = 0xB1
FRAME_SIZE = 12
_SCALE = 32767.0


def _axis_to_i16(v: float) -> int:
    v = -1.0 if v < -1.0 else 1.0 if v > 1.0 else v
    return int(round(v * _SCALE))


def encode(state: ControllerState) -> bytes:
    mask = 0
    for i, name in enumerate(BUTTON_NAMES):
        if name in state.buttons:
            mask |= 1 << i
    return struct.pack(
        "<B3s4h",
        MAGIC,
        mask.to_bytes(3, "little"),
        _axis_to_i16(state.left_stick.x),
        _axis_to_i16(state.left_stick.y),
        _axis_to_i16(state.right_stick.x),
        _axis_to_i16(state.right_stick.y),
    )


def decode(data: bytes) -> ControllerState:
    if len(data) != FRAME_SIZE or data[0] != MAGIC:
        raise ValueError("not a v1 binary input frame")
    _, mask_b, lx, ly, rx, ry = struct.unpack("<B3s4h", data)
    mask = int.from_bytes(mask_b, "little")
    buttons = frozenset(name for i, name in enumerate(BUTTON_NAMES) if mask & (1 << i))
    return ControllerState(
        buttons=buttons,
        left_stick=Stick(lx / _SCALE, ly / _SCALE).clamped(),
        right_stick=Stick(rx / _SCALE, ry / _SCALE).clamped(),
    )


def is_binary_input(data: bytes) -> bool:
    return len(data) == FRAME_SIZE and data[0] == MAGIC
