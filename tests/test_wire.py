import struct

import pytest

from nxremote.hid import wire
from nxremote.hid.state import BUTTON_NAMES, ControllerState, Stick


def test_frame_is_12_bytes_with_magic():
    f = wire.encode(ControllerState())
    assert len(f) == wire.FRAME_SIZE == 12
    assert f[0] == wire.MAGIC == 0xB1
    assert wire.is_binary_input(f)
    assert not wire.is_binary_input(b"")
    assert not wire.is_binary_input(b"\x00" * 12)  # wrong magic
    assert not wire.is_binary_input(f[:-1])        # wrong length


def test_roundtrip_buttons_and_sticks():
    s = ControllerState(
        buttons=frozenset({"A", "ZR", "DPAD_UP", "L", "HOME"}),
        left_stick=Stick(1.0, -1.0),
        right_stick=Stick(-0.5, 0.25),
    )
    d = wire.decode(wire.encode(s))
    assert d.buttons == s.buttons
    assert d.left_stick.x == pytest.approx(1.0, abs=1e-3)
    assert d.left_stick.y == pytest.approx(-1.0, abs=1e-3)
    assert d.right_stick.x == pytest.approx(-0.5, abs=1e-3)
    assert d.right_stick.y == pytest.approx(0.25, abs=1e-3)


def test_every_button_bit_is_independent():
    for name in BUTTON_NAMES:
        d = wire.decode(wire.encode(ControllerState(buttons=frozenset({name}))))
        assert d.buttons == frozenset({name}), name


def test_decode_rejects_bad_frames():
    with pytest.raises(ValueError):
        wire.decode(b"\x00" * 12)
    with pytest.raises(ValueError):
        wire.decode(wire.encode(ControllerState())[:-1])


def test_axes_are_clamped_on_decode():
    # Hand-build a frame with out-of-range int16 axes (shouldn't happen, but be safe).
    raw = bytes([wire.MAGIC, 0, 0, 0]) + struct.pack("<4h", 32767, -32767, 20000, -20000)
    d = wire.decode(raw)
    assert -1.0 <= d.left_stick.x <= 1.0 and -1.0 <= d.right_stick.y <= 1.0


def test_matches_kotlin_layout():
    """Bytes built with the Android toBinaryFrame() algorithm must decode here.

    The Kotlin order list is duplicated in ControllerState.kt; this guards the
    contract between the two languages.
    """
    kotlin_order = [
        "Y", "X", "B", "A", "R", "ZR",
        "MINUS", "PLUS", "R_STICK", "L_STICK", "HOME", "CAPTURE",
        "DPAD_DOWN", "DPAD_UP", "DPAD_RIGHT", "DPAD_LEFT", "L", "ZL",
    ]
    assert list(BUTTON_NAMES) == kotlin_order

    import math

    def kotlin_frame(buttons, lx, ly, rx, ry):
        mask = 0
        for i, n in enumerate(kotlin_order):
            if n in buttons:
                mask |= 1 << i
        i16 = lambda v: max(-32768, min(32767, math.floor(v * 32767 + 0.5)))  # noqa: E731
        return bytes([wire.MAGIC, mask & 0xFF, (mask >> 8) & 0xFF, (mask >> 16) & 0xFF]) + struct.pack(
            "<4h", i16(lx), i16(ly), i16(rx), i16(ry)
        )

    d = wire.decode(kotlin_frame({"B", "DPAD_LEFT", "ZL"}, -1.0, 1.0, 0.0, -0.5))
    assert d.buttons == frozenset({"B", "DPAD_LEFT", "ZL"})
    assert d.left_stick.x == pytest.approx(-1.0, abs=1e-3)
    assert d.right_stick.y == pytest.approx(-0.5, abs=1e-3)
