import asyncio

import pytest

from nxremote.bt import setup
from nxremote.cli import build_parser
from nxremote.hid.state import normalize_buttons
from nxremote.hub import InputHub, Overlay
from nxremote.macros import MacroRunner, Step, tap_steps
from nxremote.store import PairingStore, load_or_create_token

SHOW = (
    "ExecStart={ path=/usr/libexec/bluetooth/bluetoothd ; argv[]=/usr/libexec/bluetooth/bluetoothd ; "
    "ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }\n"
)


def test_dropin_adds_input_plugin_exclusion():
    argv = setup.parse_exec_start(SHOW)
    assert argv == ["/usr/libexec/bluetooth/bluetoothd"]
    text = setup.build_dropin(argv)
    assert text == (
        "[Service]\nExecStart=\nExecStart=/usr/libexec/bluetooth/bluetoothd --noplugin=input\n"
    )


def test_dropin_is_idempotent_and_merges_plugins():
    argv = ["/usr/lib/bluetooth/bluetoothd", "-d", "--noplugin=input"]
    assert setup.build_dropin(argv).count("--noplugin=") == 1
    argv = ["/usr/lib/bluetooth/bluetoothd", "--noplugin=sap,a2dp"]
    assert "--noplugin=sap,a2dp,input" in setup.build_dropin(argv)
    argv = ["/usr/lib/bluetooth/bluetoothd", "--noplugin=*"]
    assert "--noplugin=*" in setup.build_dropin(argv) and "input" not in setup.build_dropin(argv)


def test_parse_exec_start_rejects_garbage():
    with pytest.raises(ValueError):
        setup.parse_exec_start("nothing here")


def test_store_roundtrip(tmp_path):
    s = PairingStore(tmp_path)
    assert s.load() == []
    s.remember("aa:bb:cc:dd:ee:01")
    s.remember("AA:BB:CC:DD:EE:02")
    s.remember("AA:BB:CC:DD:EE:01")
    assert s.load() == ["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"]
    assert s.forget("aa:bb:cc:dd:ee:01") == ["AA:BB:CC:DD:EE:01"]
    assert s.load() == ["AA:BB:CC:DD:EE:02"]
    assert s.forget() == ["AA:BB:CC:DD:EE:02"] and s.load() == []


def test_token_is_persistent_and_private(tmp_path):
    t1 = load_or_create_token(tmp_path)
    assert len(t1) >= 32 and load_or_create_token(tmp_path) == t1
    assert (tmp_path / "token").stat().st_mode & 0o077 == 0


def test_cli_parser():
    a = build_parser().parse_args(["serve", "--backend", "virtual", "--port", "9", "--body-colour", "#112233"])
    assert a.backend == "virtual" and a.port == 9 and a.body_colour == bytes.fromhex("112233")


async def test_hub_overlay_layering():
    hub = InputHub()
    hub.update_direct(buttons=normalize_buttons(["A"]))
    oid = hub.push_overlay(Overlay(buttons=normalize_buttons(["B"])))
    assert hub.snapshot().buttons == {"A", "B"}
    hub.replace_overlay(oid, Overlay())
    assert hub.snapshot().buttons == {"A"}
    hub.pop_overlay(oid)
    hub.reset()
    assert hub.snapshot().buttons == frozenset()


async def test_macro_runner_fifo_timing_and_cancel():
    hub = InputHub()
    wakes = []
    hub.add_waker(lambda: wakes.append(sorted(hub.snapshot().buttons)))
    runner = MacroRunner(hub)
    runner.start()
    a = runner.submit(tap_steps(normalize_buttons(["A"]), 0.05, 0.02))
    b = runner.submit(tap_steps(normalize_buttons(["B"]), 0.05, 0.0))
    await asyncio.wait_for(b.done.wait(), 2)
    assert a.status == b.status == "done"
    assert [w for w in wakes if w] == [["A"], ["B"]]  # strictly sequential
    assert hub.snapshot().buttons == frozenset()

    long = runner.submit([Step(30.0, Overlay(buttons=normalize_buttons(["X"])))])
    await asyncio.sleep(0.05)
    assert long.status == "running" and "X" in hub.snapshot().buttons
    runner.cancel(long.id)
    await asyncio.wait_for(long.done.wait(), 1)
    assert long.status == "cancelled" and not hub.snapshot().buttons

    queued = runner.submit([Step(30.0, Overlay(buttons=normalize_buttons(["Y"])))])
    pending = runner.submit([Step(0.01)])
    await asyncio.sleep(0.05)
    assert runner.cancel_all() == 2
    await asyncio.wait_for(queued.done.wait(), 1)  # a running job stops asynchronously
    assert pending.status == "cancelled" and queued.status == "cancelled"
    with pytest.raises(ValueError):
        runner.submit([])
    await runner.close()
