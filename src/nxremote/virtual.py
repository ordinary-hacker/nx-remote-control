"""A simulated Nintendo Switch for development and tests (no Bluetooth needed).

It performs the same initial handshake a console does, records what the
controller reports, and can send rumble. ``VirtualConnector`` plugs it in as a
backend so the whole stack (core + API) can run on any machine::

    nxremote serve --backend virtual
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from typing import Optional

from .hid import rumble
from .hid.imu import ACCEL_LSB_PER_G, GYRO_LSB_PER_DPS
from .hid.protocol import HIDP_OUTPUT
from .hid.state import BUTTONS, ControllerState, Imu, Stick
from .hid.sticks import LEFT_CAL, RIGHT_CAL, StickCalibration, unpack12
from .transport import Connector, Link, SocketLink, Stopped

log = logging.getLogger(__name__)

NEUTRAL_RUMBLE = rumble.NEUTRAL_RUMBLE

# (subcommand, args) the console sends right after connecting.
HANDSHAKE = [
    (0x02, b""),  # device info
    (0x08, b"\x01"),  # shipment
    (0x10, bytes([0x00, 0x60, 0x00, 0x00, 0x10])),  # serial
    (0x10, bytes([0x50, 0x60, 0x00, 0x00, 0x0D])),  # colours
    (0x10, bytes([0x3D, 0x60, 0x00, 0x00, 0x19])),  # stick calibration
    (0x10, bytes([0x80, 0x60, 0x00, 0x00, 0x18])),  # factory params
    (0x10, bytes([0x20, 0x60, 0x00, 0x00, 0x18])),  # IMU calibration
    (0x03, b"\x30"),  # standard input mode
    (0x40, b"\x01"),  # IMU on
    (0x48, b"\x01"),  # vibration on
    (0x30, b"\x01"),  # player 1 lights
]


def _decode_axis(cal: StickCalibration, raw: tuple[int, int]) -> Stick:
    x, y = raw
    dx, dy = x - cal.center_x, y - cal.center_y
    rx = dx / (cal.above_x if dx >= 0 else cal.below_x)
    ry = dy / (cal.above_y if dy >= 0 else cal.below_y)
    return Stick(round(rx, 3), round(ry, 3))


class VirtualSwitch:
    def __init__(self, itr: socket.socket, ctrl: socket.socket, auto_rumble: bool = False) -> None:
        self.itr = itr
        self.ctrl = ctrl
        self.auto_rumble = auto_rumble
        self.handshake_done = threading.Event()
        self.closed = threading.Event()
        self._stop = threading.Event()
        self._counter = 0
        self._lock = threading.Lock()
        self.replies_by_sub: dict[int, bytes] = {}
        self.reports = 0
        self.state = ControllerState()
        self.mode_report = 0
        self._rumble = (NEUTRAL_RUMBLE, NEUTRAL_RUMBLE)
        self._thread = threading.Thread(target=self._run, name="virtual-switch", daemon=True)
        self.itr.settimeout(0.05)

    def start(self) -> "VirtualSwitch":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        for s in (self.itr, self.ctrl):
            try:
                s.close()
            except OSError:
                pass
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2)

    # -- sending -------------------------------------------------------------

    def _send_output(self, report_id: int, rumble_l: bytes, rumble_r: bytes, tail: bytes = b"") -> None:
        self._counter = (self._counter + 1) & 0x0F
        pkt = bytes((HIDP_OUTPUT, report_id, self._counter)) + rumble_l + rumble_r + tail
        pkt = pkt.ljust(50, b"\x00")
        try:
            self.itr.send(pkt)
        except OSError:
            self.closed.set()

    def send_subcommand(self, sub: int, args: bytes = b"") -> None:
        self._send_output(0x01, *self._rumble, bytes((sub,)) + args)

    def send_rumble(self, left: bytes, right: Optional[bytes] = None) -> None:
        """Send a rumble-only output report (0x10), like a game would."""
        self._rumble = (bytes(left), bytes(right if right is not None else left))
        self._send_output(0x10, *self._rumble)

    # -- receiving -----------------------------------------------------------

    def _poll(self) -> Optional[bytes]:
        try:
            data = self.itr.recv(512)
        except socket.timeout:
            return None
        except OSError:
            self.closed.set()
            return None
        if data == b"":
            self.closed.set()
            return None
        self._parse(data)
        return data

    def _parse(self, data: bytes) -> None:
        if len(data) < 2 or data[0] != 0xA1:
            return
        rid = data[1]
        self.reports += 1
        if rid in (0x21, 0x30) and len(data) >= 13:
            self._parse_standard(data)
        if rid == 0x21 and len(data) >= 16:
            with self._lock:
                self.replies_by_sub[data[15]] = bytes(data)
        self.mode_report = rid

    def _parse_standard(self, d: bytes) -> None:
        names = set()
        for name, (idx, mask) in BUTTONS.items():
            if d[4 + idx] & mask:
                names.add(name)
        left = _decode_axis(LEFT_CAL, unpack12(d[7:10]))
        right = _decode_axis(RIGHT_CAL, unpack12(d[10:13]))
        imu = self.state.imu
        if d[1] == 0x30 and len(d) >= 26 and any(d[14:26]):
            ax, ay, az, gx, gy, gz = struct.unpack_from("<6h", d, 14)
            imu = Imu(
                (round(ax / ACCEL_LSB_PER_G, 3), round(ay / ACCEL_LSB_PER_G, 3), round(az / ACCEL_LSB_PER_G, 3)),
                (round(gx / GYRO_LSB_PER_DPS, 2), round(gy / GYRO_LSB_PER_DPS, 2), round(gz / GYRO_LSB_PER_DPS, 2)),
            )
        self.state = ControllerState(frozenset(names), left, right, imu)

    def request(self, sub: int, args: bytes = b"", timeout: float = 2.0) -> bytes:
        """Send a subcommand and wait for its 0x21 reply (call after handshake)."""
        with self._lock:
            self.replies_by_sub.pop(sub, None)
        self.send_subcommand(sub, args)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if sub in self.replies_by_sub:
                    return self.replies_by_sub.pop(sub)
            time.sleep(0.005)
        raise TimeoutError(f"no reply to subcommand 0x{sub:02x}")

    # -- thread --------------------------------------------------------------

    def _run(self) -> None:
        try:
            # Wait for the controller's first report, then configure it.
            deadline = time.monotonic() + 10
            while not self._stop.is_set() and not self.reports and time.monotonic() < deadline:
                self._poll()
            for sub, args in HANDSHAKE:
                if self._stop.is_set() or self.closed.is_set():
                    return
                with self._lock:
                    self.replies_by_sub.pop(sub, None)
                self.send_subcommand(sub, args)
                t_end = time.monotonic() + 2
                while time.monotonic() < t_end and not self._stop.is_set():
                    self._poll()
                    with self._lock:
                        if sub in self.replies_by_sub:
                            break
            self.handshake_done.set()

            a_down = False
            pulse_end = 0.0
            while not self._stop.is_set() and not self.closed.is_set():
                self._poll()
                if self.auto_rumble:
                    now = time.monotonic()
                    pressed = "A" in self.state.buttons
                    if pressed and not a_down:
                        self.send_rumble(rumble.encode(320, 0.8, 160, 0.8))
                        pulse_end = now + 0.25
                    elif pulse_end and now >= pulse_end:
                        self.send_rumble(NEUTRAL_RUMBLE)
                        pulse_end = 0.0
                    a_down = pressed
        except Exception:  # pragma: no cover - debugging aid
            log.exception("virtual switch crashed")
        finally:
            self.closed.set()


class VirtualConnector(Connector):
    """Backend that connects to an in-process :class:`VirtualSwitch`."""

    address = "7C:BB:8A:00:00:01"

    def __init__(self, auto_rumble: bool = False) -> None:
        self.auto_rumble = auto_rumble
        self.switch: Optional[VirtualSwitch] = None
        self.connections = 0

    def _make(self) -> Link:
        c_itr, s_itr = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        c_ctrl, s_ctrl = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        if self.switch is not None:
            self.switch.stop()
        self.switch = VirtualSwitch(s_itr, s_ctrl, self.auto_rumble)
        self.connections += 1
        self.switch.start()
        return SocketLink(c_itr, c_ctrl, "7C:BB:8A:FF:00:01")

    def listen(self, stop: threading.Event) -> Link:
        if stop.is_set():
            raise Stopped()
        time.sleep(0.2)
        return self._make()

    def connect(self, address: str, stop: threading.Event) -> Link:
        return self._make()

    def cleanup(self) -> None:
        if self.switch is not None:
            self.switch.stop()
            self.switch = None
