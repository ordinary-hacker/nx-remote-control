"""End-to-end: client (real transport + runner) -> live nxremote host (virtual Switch).

Starts the host's FastAPI app on a real uvicorn server bound to a random local
port, with the in-process virtual Switch backend, then forwards a programmable
StaticSource through the actual WebSocket transport and checks the inputs land
on the virtual console and that rumble comes back to a fake sink.

Skipped automatically if the host package (nxremote) is not importable.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest

nxremote = pytest.importorskip("nxremote")

from nxremote.api.app import create_app  # noqa: E402
from nxremote.core import ControllerService  # noqa: E402
from nxremote.hid import rumble as host_rumble  # noqa: E402
from nxremote.hub import InputHub  # noqa: E402
from nxremote.store import PairingStore  # noqa: E402
from nxremote.virtual import VirtualConnector  # noqa: E402

from nxremote_client.runner import ForwarderClient  # noqa: E402
from nxremote_client.sources import InputSource, RumbleSink, StaticSource  # noqa: E402
from nxremote_client.state import ControllerState, RumbleState  # noqa: E402
from nxremote_client.transport import Transport, build_ws_url  # noqa: E402

TOKEN = "e2e-token"


class RecordingSink(RumbleSink):
    def __init__(self) -> None:
        self.events: list[RumbleState] = []

    def rumble(self, state: RumbleState) -> None:
        self.events.append(state)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _await(cond, timeout=8.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def host_server():
    import uvicorn

    hub = InputHub()
    conn = VirtualConnector()
    svc = ControllerService(conn, hub, PairingStore_tmp())
    app = create_app(svc, hub, token=TOKEN, autostart=True)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    # Wait for the socket to accept connections.
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started
    yield port, svc, conn
    server.should_exit = True
    thread.join(timeout=10)


def PairingStore_tmp():
    import tempfile
    from pathlib import Path

    return PairingStore(Path(tempfile.mkdtemp()))


async def test_client_forwards_inputs_and_receives_rumble(host_server):
    port, svc, conn = host_server
    await _await(lambda: svc.state == "connected", what="host connected to virtual switch")
    await _await(lambda: conn.switch and conn.switch.handshake_done.is_set(), what="handshake")
    sw = conn.switch

    source = StaticSource("fake pad")
    sink = RecordingSink()
    url = build_ws_url(f"127.0.0.1:{port}", token=TOKEN)
    transport = Transport(url, reconnect=False)
    client = ForwarderClient(source, transport, sink, rate=200.0, keepalive=0.2)

    run_task = asyncio.create_task(client.run())
    try:
        await transport.wait_connected(timeout=8)

        # Press buttons + sticks on the fake pad; expect them on the virtual console.
        source.set_state(ControllerState(frozenset({"A", "ZR", "DPAD_UP"}), lx=1.0, ly=-1.0, rx=-1.0, ry=1.0))
        await _await(lambda: sw.state.buttons == {"A", "ZR", "DPAD_UP"}, what="buttons on console")
        await _await(lambda: sw.state.left_stick.x > 0.9 and sw.state.left_stick.y < -0.9, what="left stick")
        assert sw.state.right_stick.x < -0.9 and sw.state.right_stick.y > 0.9

        # Release everything.
        source.set_state(ControllerState())
        await _await(lambda: not sw.state.buttons and abs(sw.state.left_stick.x) < 0.05, what="release")

        # The console rumbles; the client's sink should receive a non-zero then zero event.
        sw.send_rumble(host_rumble.encode(320, 0.9, 160, 0.9), host_rumble.NEUTRAL_RUMBLE)
        await _await(lambda: any(e.active for e in sink.events), what="rumble received by client")
        strong = [e for e in sink.events if e.active][-1]
        assert strong.left > 0.8
        sw.send_rumble(host_rumble.NEUTRAL_RUMBLE)
        await _await(lambda: sink.events and not sink.events[-1].active, what="rumble stop received")
    finally:
        client.stop()
        await asyncio.wait_for(run_task, timeout=10)


async def test_client_release_on_close(host_server):
    port, svc, conn = host_server
    await _await(lambda: conn.switch and conn.switch.handshake_done.is_set(), what="handshake")
    sw = conn.switch

    source = StaticSource("fake pad")
    transport = Transport(build_ws_url(f"127.0.0.1:{port}", token=TOKEN), reconnect=False)
    client = ForwarderClient(source, transport, None, rate=200.0)
    run_task = asyncio.create_task(client.run())
    try:
        await transport.wait_connected(timeout=8)
        source.set_state(ControllerState(frozenset({"B"})))
        await _await(lambda: "B" in sw.state.buttons, what="held")
    finally:
        client.stop()
        await asyncio.wait_for(run_task, timeout=10)
    # The host's dead-man's switch releases inputs when the forwarding socket closes.
    await _await(lambda: not sw.state.buttons, what="released on client disconnect")


async def test_stop_terminates_live_session_promptly(host_server):
    port, svc, conn = host_server
    transport = Transport(build_ws_url(f"127.0.0.1:{port}", token=TOKEN), reconnect=True, reconnect_delay=0.2)
    run_task = asyncio.create_task(transport.run())
    try:
        await transport.wait_connected(timeout=8)
        assert transport.connected
    finally:
        t0 = time.monotonic()
        transport.stop()
        # stop() closes the live socket, so run() returns quickly (well under the
        # 20s ping timeout) even though the read loop was blocked in recv().
        await asyncio.wait_for(run_task, timeout=5)
        assert time.monotonic() - t0 < 4
    assert not transport.connected


async def test_transport_reconnects_after_drop(host_server):
    port, svc, conn = host_server
    reconnects = []
    transport = Transport(
        build_ws_url(f"127.0.0.1:{port}", token=TOKEN),
        reconnect=True,
        reconnect_delay=0.2,
        on_connected=lambda hello: reconnects.append(time.monotonic()),
    )
    run_task = asyncio.create_task(transport.run())
    try:
        await transport.wait_connected(timeout=8)
        # Drop the live socket from the client side; the transport should reconnect.
        ws = transport._ws
        assert ws is not None
        await ws.close()
        await _await(lambda: len(reconnects) >= 2, what="second connection after drop")
        assert transport.connected
    finally:
        transport.stop()
        await asyncio.wait_for(run_task, timeout=10)


async def test_bad_token_does_not_crash(host_server):
    port, svc, conn = host_server
    transport = Transport(build_ws_url(f"127.0.0.1:{port}", token="wrong"), reconnect=False)
    run_task = asyncio.create_task(transport.run())
    # Should finish (connection rejected) without raising.
    await asyncio.wait_for(run_task, timeout=10)
    assert not transport.connected
