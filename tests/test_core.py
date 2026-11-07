import time

import pytest

from nxremote.core import ControllerService
from nxremote.hid import rumble
from nxremote.hid.state import Imu, Stick, normalize_buttons
from nxremote.hub import InputHub, Overlay
from nxremote.store import PairingStore
from nxremote.virtual import VirtualConnector


def wait_for(cond, timeout=8.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def rig(tmp_path):
    hub = InputHub()
    conn = VirtualConnector()
    svc = ControllerService(conn, hub, PairingStore(tmp_path))
    events = []
    svc.add_listener(lambda n, p: events.append((n, p)))
    yield svc, hub, conn, events
    svc.stop()


def connect(svc, conn):
    assert svc.start(reconnect=False)
    wait_for(lambda: svc.state == "connected", what="connected")
    wait_for(lambda: conn.switch.handshake_done.is_set(), what="handshake")
    return conn.switch


def test_handshake_and_connected(rig):
    svc, hub, conn, events = rig
    sw = connect(svc, conn)
    st = svc.status()
    assert st["connected"] and st["mode"] == "standard"
    assert st["imu_enabled"] and st["vibration_enabled"]
    assert st["player_lights"]["player"] == 1
    assert svc.store.load() == [svc.peer]
    assert "state" in [n for n, _ in events]
    assert sw.mode_report in (0x21, 0x30)


def test_inputs_reach_console(rig):
    svc, hub, conn, _ = rig
    sw = connect(svc, conn)
    hub.update_direct(
        buttons=normalize_buttons(["a", "zr", "up"]),
        left_stick=Stick(0.5, -1.0),
        right_stick=Stick(-1.0, 1.0),
        imu=Imu(accel=(0.1, 0.0, 0.9), gyro=(30.0, 0.0, -15.0)),
    )
    wait_for(lambda: sw.state.buttons == {"A", "ZR", "DPAD_UP"}, what="buttons")
    wait_for(lambda: sw.state.left_stick.y == -1.0, what="stick")
    s = sw.state
    assert s.left_stick.x == pytest.approx(0.5, abs=0.01)
    assert s.right_stick.x == pytest.approx(-1.0, abs=0.01) and s.right_stick.y == pytest.approx(1.0, abs=0.01)
    assert s.imu.accel[0] == pytest.approx(0.1, abs=0.01) and s.imu.accel[2] == pytest.approx(0.9, abs=0.01)
    assert s.imu.gyro[0] == pytest.approx(30, abs=0.2) and s.imu.gyro[2] == pytest.approx(-15, abs=0.2)
    hub.reset()
    wait_for(lambda: not sw.state.buttons and sw.state.left_stick.y == 0, what="release")


def test_short_tap_via_overlay_is_not_lost(rig):
    svc, hub, conn, _ = rig
    sw = connect(svc, conn)
    seen = []
    orig = sw._parse_standard

    def spy(d):
        orig(d)
        seen.append("A" in sw.state.buttons)

    sw._parse_standard = spy
    oid = hub.push_overlay(Overlay(buttons=normalize_buttons(["A"])))
    time.sleep(0.04)
    hub.pop_overlay(oid)
    time.sleep(0.1)
    assert True in seen and seen[-1] is False


def test_rumble_reaches_listeners(rig):
    svc, hub, conn, events = rig
    sw = connect(svc, conn)
    sw.send_rumble(rumble.encode(320, 0.9, 160, 0.9), rumble.NEUTRAL_RUMBLE)
    wait_for(lambda: any(n == "rumble" for n, _ in events), what="rumble event")
    ev = [p for n, p in events if n == "rumble"][-1]
    assert ev["left"]["active"] and ev["left"]["intensity"] > 0.8
    assert not ev["right"]["active"]
    assert svc.status()["last_rumble"]["left"]["active"]
    sw.send_rumble(rumble.NEUTRAL_RUMBLE)
    wait_for(lambda: not [p for n, p in events if n == "rumble"][-1]["left"]["active"], what="rumble off")


def test_console_subcommand_after_connect_is_answered(rig):
    svc, hub, conn, events = rig
    sw = connect(svc, conn)
    r = sw.request(0x30, b"\x0f")
    assert r[14] == 0x80 and r[15] == 0x30
    wait_for(lambda: svc.status()["player_lights"]["player"] == 4, what="lights")
    r = sw.request(0x02)
    assert r[18] == 0x03


def test_reconnects_after_console_drops(rig):
    svc, hub, conn, _ = rig
    sw = connect(svc, conn)
    first = sw
    sw.stop()  # console vanishes
    wait_for(lambda: conn.connections >= 2 and conn.switch is not first, what="second connection")
    wait_for(lambda: conn.switch.handshake_done.is_set(), what="second handshake")
    wait_for(lambda: svc.state == "connected", what="reconnected")


def test_stop_and_restart(rig):
    svc, hub, conn, _ = rig
    connect(svc, conn)
    hub.update_direct(buttons=normalize_buttons(["A"]))
    svc.stop()
    assert svc.state == "stopped"
    assert hub.direct().buttons == frozenset()  # released on stop
    connect(svc, conn)
    assert svc.state == "connected"
