import pytest

from nxremote_client.mapping import (
    SDL_A,
    SDL_B,
    SDL_BACK,
    SDL_DPAD_UP,
    SDL_GUIDE,
    SDL_LSHOULDER,
    SDL_MISC1,
    SDL_START,
    SDL_X,
    SDL_Y,
    Mapping,
)
from nxremote_client.state import NEUTRAL, ControllerState, RumbleState
from nxremote_client.transport import build_ws_url


def test_positional_mapping_keeps_physical_layout():
    m = Mapping(face="positional")
    assert m.button(SDL_A) == "B"  # bottom button -> Switch B
    assert m.button(SDL_B) == "A"
    assert m.button(SDL_X) == "Y"
    assert m.button(SDL_Y) == "X"


def test_label_mapping_matches_printed_letters():
    m = Mapping(face="labels")
    assert m.button(SDL_A) == "A"
    assert m.button(SDL_B) == "B"
    assert m.button(SDL_X) == "X"
    assert m.button(SDL_Y) == "Y"


def test_common_buttons_independent_of_face():
    for face in ("positional", "labels"):
        m = Mapping(face=face)
        assert m.button(SDL_BACK) == "MINUS"
        assert m.button(SDL_START) == "PLUS"
        assert m.button(SDL_GUIDE) == "HOME"
        assert m.button(SDL_MISC1) == "CAPTURE"
        assert m.button(SDL_LSHOULDER) == "L"
        assert m.button(SDL_DPAD_UP) == "DPAD_UP"


def test_swap_options():
    m = Mapping(face="labels", swap_ab=True, swap_xy=True)
    assert m.button(SDL_A) == "B" and m.button(SDL_B) == "A"
    assert m.button(SDL_X) == "Y" and m.button(SDL_Y) == "X"


def test_button_names_are_all_valid_switch_names():
    from nxremote_client.state import SWITCH_BUTTONS

    for name in Mapping().button_table().values():
        assert name in SWITCH_BUTTONS


def test_state_normalization_and_message():
    s = ControllerState(frozenset({"A", "ZR"}), lx=2.0, ly=-3.0, rx=0.5, ry=0.0)
    n = s.normalized()
    assert n.lx == 1.0 and n.ly == -1.0 and n.rx == 0.5
    # to_input_message() rounds but does not clamp; the transport sends
    # normalized() state, so out-of-range raw values never reach the wire.
    assert n.to_input_message()["left_stick"] == {"x": 1.0, "y": -1.0}
    msg = s.to_input_message()
    assert msg["type"] == "input" and msg["buttons"] == ["A", "ZR"]
    assert msg["left_stick"] == {"x": 2.0, "y": -3.0}
    assert msg["right_stick"] == {"x": 0.5, "y": 0.0}


def test_state_equality_drives_diffing():
    assert ControllerState(frozenset({"A"})) == ControllerState(frozenset({"A"}))
    assert ControllerState(frozenset({"A"})) != ControllerState(frozenset({"B"}))
    assert NEUTRAL == ControllerState()


def test_rumble_state_active():
    assert not RumbleState().active
    assert RumbleState(left=0.1).active
    assert RumbleState(right=0.3).active


@pytest.mark.parametrize(
    "host,expected",
    [
        ("1.2.3.4", "ws://1.2.3.4:8000/v1/ws"),
        ("1.2.3.4:9000", "ws://1.2.3.4:9000/v1/ws"),
        ("http://pi:8000", "ws://pi:8000/v1/ws"),
        ("https://pi", "wss://pi:8000/v1/ws"),
        ("wss://pi/v1/ws", "wss://pi:8000/v1/ws"),
        ("pi:8000/", "ws://pi:8000/v1/ws"),
    ],
)
def test_build_ws_url(host, expected):
    assert build_ws_url(host) == expected


def test_build_ws_url_token_and_release():
    assert build_ws_url("pi:8000", token="a/b c") == "ws://pi:8000/v1/ws?token=a%2Fb%20c"
    assert build_ws_url("pi", token="t", release_on_close=False) == "ws://pi:8000/v1/ws?token=t&release_on_close=false"


def test_build_ws_url_rejects_bad_scheme():
    with pytest.raises(ValueError):
        build_ws_url("ftp://pi")
