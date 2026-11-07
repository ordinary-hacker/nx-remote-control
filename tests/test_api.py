import time

import pytest
from fastapi.testclient import TestClient

from nxremote.api.app import create_app
from nxremote.core import ControllerService
from nxremote.hid import rumble
from nxremote.hub import InputHub
from nxremote.store import PairingStore
from nxremote.virtual import VirtualConnector

TOKEN = "s3cret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def wait_for(cond, timeout=8.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def env(tmp_path):
    hub = InputHub()
    conn = VirtualConnector()
    svc = ControllerService(conn, hub, PairingStore(tmp_path))
    app = create_app(svc, hub, token=TOKEN, autostart=True)
    with TestClient(app) as client:
        wait_for(lambda: svc.state == "connected", what="connected")
        wait_for(lambda: conn.switch.handshake_done.is_set(), what="handshake")
        yield client, svc, hub, conn


def test_auth_required(env):
    client, *_ = env
    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/v1/status", headers=AUTH).status_code == 200
    assert client.get("/v1/status", headers={"X-API-Key": TOKEN}).status_code == 200
    assert client.get(f"/v1/status?token={TOKEN}").status_code == 200


def test_status(env):
    client, *_ = env
    s = client.get("/v1/status", headers=AUTH).json()
    assert s["controller"]["connected"] is True
    assert s["controller"]["vibration_enabled"] and s["controller"]["imu_enabled"]
    assert s["controller"]["player_lights"]["player"] == 1


def test_patch_input_reaches_console(env):
    client, svc, hub, conn = env
    sw = conn.switch
    r = client.patch(
        "/v1/input",
        headers=AUTH,
        json={"press": ["a", "ZR"], "left_stick": {"x": -1, "y": 0.5}, "imu": {"gyro": [0, 90, 0]}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["direct"]["buttons"] == ["A", "ZR"]
    wait_for(lambda: sw.state.buttons == {"A", "ZR"} and sw.state.left_stick.x == -1.0, what="inputs")
    assert sw.state.imu.gyro[1] == pytest.approx(90, abs=0.2)
    client.patch("/v1/input", headers=AUTH, json={"release": ["A"]})
    wait_for(lambda: sw.state.buttons == {"ZR"}, what="release A")
    client.put("/v1/input", headers=AUTH, json={"buttons": ["X"]})
    wait_for(lambda: sw.state.buttons == {"X"} and sw.state.left_stick.x == 0, what="put replaces")
    client.post("/v1/input/release-all", headers=AUTH)
    wait_for(lambda: not sw.state.buttons, what="release-all")


def test_validation_errors(env):
    client, *_ = env
    assert client.patch("/v1/input", headers=AUTH, json={"press": ["bogus"]}).status_code == 422
    assert client.patch("/v1/input", headers=AUTH, json={"left_stick": {"x": 2}}).status_code == 422
    assert client.patch("/v1/input", headers=AUTH, json={"imu": {"gyro": [0, 0, 5000]}}).status_code == 422
    assert client.patch("/v1/input", headers=AUTH, json={"nonsense": 1}).status_code == 422
    assert client.post("/v1/tap", headers=AUTH, json={"buttons": []}).status_code == 422


def test_tap_wait_and_macro(env):
    client, svc, hub, conn = env
    sw = conn.switch
    seen = []
    orig = sw._parse_standard

    def spy(d):
        orig(d)
        seen.append(frozenset(sw.state.buttons))

    sw._parse_standard = spy
    t0 = time.monotonic()
    r = client.post("/v1/tap", headers=AUTH, json={"buttons": ["a"], "hold_ms": 120, "release_ms": 30, "wait": True})
    assert r.status_code == 202 and r.json()["status"] == "done"
    assert 0.12 <= time.monotonic() - t0 < 1.0
    assert frozenset({"A"}) in seen and seen[-1] == frozenset()

    seen.clear()
    r = client.post(
        "/v1/macros",
        headers=AUTH,
        json={
            "wait": True,
            "repeat": 2,
            "steps": [
                {"buttons": ["B"], "left_stick": {"x": 1}, "duration_ms": 60},
                {"buttons": [], "duration_ms": 40},
            ],
        },
    )
    assert r.json()["status"] == "done" and r.json()["iteration"] == 2
    assert frozenset({"B"}) in seen
    assert hub.snapshot().buttons == frozenset()


def test_macro_cancel(env):
    client, *_ = env
    r = client.post("/v1/macros", headers=AUTH, json={"steps": [{"buttons": ["Y"], "duration_ms": 30000}]})
    jid = r.json()["id"]
    wait_for(lambda: client.get(f"/v1/macros/{jid}", headers=AUTH).json()["status"] == "running", what="running")
    assert client.delete(f"/v1/macros/{jid}", headers=AUTH).json()["cancelled"] is True
    wait_for(lambda: client.get(f"/v1/macros/{jid}", headers=AUTH).json()["status"] == "cancelled", what="cancel")
    assert client.get("/v1/macros/zzz", headers=AUTH).status_code == 404


def test_websocket_input_rumble_and_release_on_close(env):
    client, svc, hub, conn = env
    sw = conn.switch
    with client.websocket_connect(f"/v1/ws?token={TOKEN}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["controller"]["connected"]
        ws.send_json({"type": "input", "id": 1, "press": ["A"], "right_stick": {"x": 1, "y": 0}})
        assert ws.receive_json() == {"type": "ack", "id": 1}
        wait_for(lambda: "A" in sw.state.buttons and sw.state.right_stick.x == pytest.approx(1, abs=0.01), what="ws input")
        ws.send_json({"type": "input", "id": 2, "press": ["nope"]})
        err = ws.receive_json()
        assert err["type"] == "error" and err["id"] == 2
        ws.send_json({"type": "ping", "id": 3})
        assert ws.receive_json() == {"type": "pong", "id": 3}

        sw.send_rumble(rumble.encode(400, 1.0, 200, 0.7), rumble.NEUTRAL_RUMBLE)
        ev = ws.receive_json()
        while ev["type"] != "rumble":
            ev = ws.receive_json()
        assert ev["left"]["active"] and ev["left"]["intensity"] >= 0.95
        assert ev["left"]["high"]["frequency"] == pytest.approx(400, rel=0.05)
        assert not ev["right"]["active"]
    # dropping the socket releases what it held
    wait_for(lambda: not sw.state.buttons, what="release on close")


def test_websocket_rejects_bad_token(env):
    client, *_ = env
    with pytest.raises(Exception):
        with client.websocket_connect("/v1/ws?token=wrong"):
            pass


def test_rumble_endpoint_and_stop_start(env):
    client, svc, hub, conn = env
    conn.switch.send_rumble(rumble.encode(320, 0.6, 160, 0.6))
    wait_for(lambda: client.get("/v1/rumble", headers=AUTH).json()["last"] is not None, what="rumble")
    assert client.get("/v1/rumble", headers=AUTH).json()["last"]["left"]["active"]

    assert client.post("/v1/connection/stop", headers=AUTH).json()["state"] == "stopped"
    assert client.post("/v1/tap", headers=AUTH, json={"buttons": ["a"]}).status_code == 409
    assert client.post("/v1/connection/start", headers=AUTH, json={}).status_code == 202
    wait_for(lambda: svc.state == "connected", what="restart")
