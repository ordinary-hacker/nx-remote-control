import struct

import pytest

from nxremote.hid import rumble
from nxremote.hid.imu import ACCEL_LSB_PER_G, encode_sample
from nxremote.hid.protocol import ProControllerProtocol
from nxremote.hid.state import (
    BUTTONS,
    ControllerState,
    Imu,
    InvalidButton,
    Stick,
    normalize_button,
)
from nxremote.hid.sticks import LEFT_CAL, RIGHT_CAL, encode_stick, pack12, unpack12

MAC = "7C:BB:8A:12:34:56"


def out_report(sub: int, args: bytes = b"", rid: int = 0x01, rumble_l=rumble.NEUTRAL_RUMBLE, rumble_r=rumble.NEUTRAL_RUMBLE):
    return (bytes((0xA2, rid, 0)) + rumble_l + rumble_r + bytes((sub,)) + args).ljust(50, b"\0")


# ---- buttons / sticks ---------------------------------------------------------


def test_button_bits_are_unique_and_match_spec():
    seen = set()
    for name, (idx, mask) in BUTTONS.items():
        assert (idx, mask) not in seen
        seen.add((idx, mask))
    assert BUTTONS["A"] == (0, 0x08) and BUTTONS["ZR"] == (0, 0x80)
    assert BUTTONS["HOME"] == (1, 0x10) and BUTTONS["CAPTURE"] == (1, 0x20)
    assert BUTTONS["ZL"] == (2, 0x80) and BUTTONS["DPAD_DOWN"] == (2, 0x01)


def test_button_aliases():
    assert normalize_button("up") == "DPAD_UP"
    assert normalize_button("ls") == "L_STICK"
    with pytest.raises(InvalidButton):
        normalize_button("turbo")


def test_pack_unpack_roundtrip():
    for x, y in [(0, 0), (4095, 4095), (2159, 1916), (1, 4094), (0x123, 0xABC)]:
        assert unpack12(pack12(x, y)) == (x, y)


def test_stick_calibration_center_and_extremes():
    assert unpack12(encode_stick(LEFT_CAL, Stick())) == (LEFT_CAL.center_x, LEFT_CAL.center_y)
    assert unpack12(encode_stick(RIGHT_CAL, Stick(1, 1))) == (
        RIGHT_CAL.center_x + RIGHT_CAL.above_x,
        RIGHT_CAL.center_y + RIGHT_CAL.above_y,
    )
    assert unpack12(encode_stick(RIGHT_CAL, Stick(-1, -1))) == (
        RIGHT_CAL.center_x - RIGHT_CAL.below_x,
        RIGHT_CAL.center_y - RIGHT_CAL.below_y,
    )
    # out of range input is clamped
    assert encode_stick(LEFT_CAL, Stick(5, -5)) == encode_stick(LEFT_CAL, Stick(1, -1))


# ---- rumble ----------------------------------------------------------------------


def test_rumble_neutral_is_off_160hz_320hz():
    r = rumble.decode(rumble.NEUTRAL_RUMBLE)
    assert not r.active
    assert r.low.frequency == pytest.approx(160, abs=0.5)
    assert r.high.frequency == pytest.approx(320, abs=0.5)


def test_rumble_decode_known_table_entries():
    # From dekuNukem's table: hf 0x01a8 / amp 0x88 ; lf 0x63 / amp 0x804d
    data = bytes((0xA8, 0x88 + 0x01, 0x63 + 0x80, 0x4D))
    r = rumble.decode(data)
    assert r.high.frequency == pytest.approx(10 * 2 ** ((0x1A8 // 4 + 0x60) / 32), rel=1e-6)
    assert r.high.amplitude == pytest.approx(0.501, abs=0.01)  # HA 0x88
    assert r.low.amplitude == pytest.approx(0.2, abs=0.2)
    assert r.active


def test_rumble_encode_decode_roundtrip():
    for f, a in [(320, 0.5), (160, 1.0), (600, 0.8), (80, 0.3), (1000, 0.15)]:
        d = rumble.decode(rumble.encode(f, a, f / 2 + 40, a))
        assert d.high.amplitude == pytest.approx(a, rel=0.08, abs=0.02)
        assert d.low.amplitude == pytest.approx(a, rel=0.08, abs=0.02)
        assert d.high.frequency == pytest.approx(f, rel=0.04)


def test_rumble_zero_amp_inactive():
    assert not rumble.decode(rumble.encode(500, 0.0, 200, 0.0)).active


# ---- protocol --------------------------------------------------------------------------


def make_proto(events=None):
    return ProControllerProtocol(MAC, on_event=lambda n, p: events.append((n, p)) if events is not None else None)


def test_device_info_reply():
    p = make_proto()
    s = ControllerState()
    r = p.handle_output(out_report(0x02), s)
    assert r[0] == 0xA1 and r[1] == 0x21 and len(r) == 50
    assert r[14] == 0x82 and r[15] == 0x02
    assert r[18] == 0x03  # Pro Controller
    assert r[20:26] == bytes.fromhex(MAC.replace(":", ""))
    assert p.device_info_queried


def test_spi_read_colours_and_calibration():
    p = ProControllerProtocol(MAC, body_colour=b"\x01\x02\x03", button_colour=b"\x04\x05\x06")
    s = ControllerState()
    r = p.handle_output(out_report(0x10, bytes([0x50, 0x60, 0, 0, 12])), s)
    assert r[14] == 0x90 and r[15] == 0x10
    assert r[16:20] == bytes([0x50, 0x60, 0, 0]) and r[20] == 12
    assert r[21:27] == b"\x01\x02\x03\x04\x05\x06"
    # factory stick calibration decodes back to our calibration values
    r = p.handle_output(out_report(0x10, bytes([0x3D, 0x60, 0, 0, 25])), s)
    assert unpack12(r[21:24]) == (LEFT_CAL.above_x, LEFT_CAL.above_y)
    assert unpack12(r[24:27]) == (LEFT_CAL.center_x, LEFT_CAL.center_y)
    assert unpack12(r[30:33]) == (RIGHT_CAL.center_x, RIGHT_CAL.center_y)
    # unknown areas read as 0xFF
    r = p.handle_output(out_report(0x10, bytes([0x00, 0x80, 0, 0, 4])), s)
    assert r[21:25] == b"\xff" * 4


def test_input_report_encodes_buttons_and_sticks_after_handshake():
    p = make_proto()
    s = ControllerState(buttons=frozenset({"A", "ZL", "HOME", "DPAD_LEFT"}), left_stick=Stick(1, 0))
    # before the console queried device info the controller looks idle
    idle = p.build_input_report(s)  # simple (0x3F) mode: buttons[2], hat, ...
    assert idle[1] == 0x3F and idle[2:4] == b"\0\0" and idle[4] == 8
    p.handle_output(out_report(0x02), s)
    p.handle_output(out_report(0x03, b"\x30"), s)
    rep = p.build_input_report(s)
    assert rep[1] == 0x30 and len(rep) == 50
    assert rep[4] == 0x08 and rep[5] == 0x10 and rep[6] == 0x80 | 0x08
    assert unpack12(rep[7:10])[0] == LEFT_CAL.center_x + LEFT_CAL.above_x


def test_imu_only_reported_when_enabled():
    p = make_proto()
    s = ControllerState(imu=Imu(accel=(0, 0, 1), gyro=(10, 0, 0)))
    p.handle_output(out_report(0x02), s)
    p.handle_output(out_report(0x03, b"\x30"), s)
    assert p.build_input_report(s)[14:50] == bytes(36)
    p.handle_output(out_report(0x40, b"\x01"), s)
    rep = p.build_input_report(s)
    ax, ay, az, gx, gy, gz = struct.unpack_from("<6h", rep, 14)
    assert az == ACCEL_LSB_PER_G and ax == 0 and gx > 0
    assert rep[14:26] == rep[26:38] == rep[38:50] == encode_sample(s.imu)


def test_player_lights_vibration_events_and_handshake():
    ev = []
    p = make_proto(ev)
    s = ControllerState()
    assert not p.handshake_complete
    p.handle_output(out_report(0x48, b"\x01"), s)
    p.handle_output(out_report(0x30, b"\x03"), s)
    assert p.handshake_complete
    assert p.player_lights.player_number == 2
    names = [n for n, _ in ev]
    assert "vibration_enabled" in names and "player_lights" in names


def test_rumble_events_only_when_enabled_and_changed():
    ev = []
    p = make_proto(ev)
    s = ControllerState()
    on = rumble.encode(320, 0.9, 160, 0.9)
    p.handle_output(out_report(0x10, rid=0x10, rumble_l=on, rumble_r=on), s)
    assert not [e for e in ev if e[0] == "rumble"]  # vibration not yet enabled
    p.handle_output(out_report(0x48, b"\x01"), s)
    p.handle_output(out_report(0x10, rid=0x10, rumble_l=on, rumble_r=rumble.NEUTRAL_RUMBLE), s)
    p.handle_output(out_report(0x10, rid=0x10, rumble_l=on, rumble_r=rumble.NEUTRAL_RUMBLE), s)  # unchanged
    rum = [e for e in ev if e[0] == "rumble"]
    assert len(rum) == 1
    assert rum[0][1]["left"]["active"] and not rum[0][1]["right"]["active"]
    # rumble also rides along on subcommand packets (report 0x01)
    p.handle_output(out_report(0x30, b"\x01", rumble_l=rumble.NEUTRAL_RUMBLE), s)
    assert len([e for e in ev if e[0] == "rumble"]) == 2


def test_simple_mode_report():
    p = make_proto()
    s = ControllerState(buttons=frozenset({"A", "DPAD_UP", "DPAD_RIGHT"}), left_stick=Stick(0, 1))
    p.handle_output(out_report(0x02), s)
    rep = p.build_input_report(s)
    assert rep[1] == 0x3F and len(rep) == 13
    assert rep[2] & 0x02  # A
    assert rep[4] == 1  # up-right
    lx, ly, rx, ry = struct.unpack_from("<4H", rep, 5)
    assert lx == 0x8000 and ly < 0x100 and rx == 0x8000


def test_misc_subcommands_reply():
    p = make_proto()
    s = ControllerState()
    for sub, ack in [(0x00, 0x80), (0x03, 0x80), (0x04, 0x83), (0x08, 0x80), (0x22, 0x80), (0x40, 0x80), (0x41, 0x80),
                     (0x48, 0x82), (0x30, 0x80), (0x38, 0x80), (0x21, 0xA0), (0x50, 0xD0), (0x31, 0xB0)]:
        r = p.handle_output(out_report(sub, b"\x01\x00\x00\x00\x00"), s)
        assert r is not None and r[14] == ack and r[15] == sub, hex(sub)
    assert p.handle_output(out_report(0x7E), s) is None  # unknown: ignored
    assert p.handle_output(b"", s) is None
    assert p.handle_output(b"\xa2", s) is None


def test_hci_disconnect_request_flag():
    p = make_proto()
    p.handle_output(out_report(0x06, b"\x00"), ControllerState())
    assert p.disconnect_requested
