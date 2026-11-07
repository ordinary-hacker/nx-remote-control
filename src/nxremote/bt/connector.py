"""Bluetooth Classic HID backend: BlueZ + raw L2CAP sockets (PSM 17 control, 19 interrupt)."""

from __future__ import annotations

import logging
import select
import socket
import threading
import time
from typing import Optional

from ..transport import Connector, Link, SocketLink, Stopped
from .bluez import BlueZ, BlueZError

log = logging.getLogger(__name__)

PSM_CONTROL = 17
PSM_INTERRUPT = 19
SWITCH_ALIAS = "Nintendo Switch"
CONNECT_TIMEOUT = 6.0


def _l2cap_socket() -> socket.socket:
    return socket.socket(
        family=socket.AF_BLUETOOTH, type=socket.SOCK_SEQPACKET, proto=socket.BTPROTO_L2CAP
    )


class BluetoothConnector(Connector):
    def __init__(self, adapter: str = "hci0", alias: str = "Pro Controller") -> None:
        self.adapter = adapter
        self.alias = alias
        self.bz: Optional[BlueZ] = None
        self.address = "00:00:00:00:00:00"
        self._watch: dict[str, int] = {}
        self._seen_connected: set[str] = set()

    # -- setup -----------------------------------------------------------------

    def prepare(self) -> None:
        if not hasattr(socket, "AF_BLUETOOTH") or not hasattr(socket, "BTPROTO_L2CAP"):
            raise OSError("this Python build has no Bluetooth socket support (need Linux with BlueZ headers)")
        if self.bz is None:
            self.bz = BlueZ(self.adapter)
        self.address = self.bz.address
        self.bz.configure(self.alias)
        self.bz.register_hid_profile()
        log.info("adapter %s (%s) configured as %r", self.adapter, self.address, self.alias)

    def cleanup(self) -> None:
        if self.bz is not None:
            try:
                self.bz.set_discoverable(False)
            except BlueZError:
                pass

    # -- connecting ------------------------------------------------------------------

    def listen(self, stop: threading.Event) -> Link:
        assert self.bz is not None
        while not stop.is_set():
            s_ctrl, s_itr = _l2cap_socket(), _l2cap_socket()
            try:
                try:
                    s_ctrl.bind((self.address, PSM_CONTROL))
                    s_itr.bind((self.address, PSM_INTERRUPT))
                except PermissionError:
                    raise
                except OSError:
                    s_ctrl.bind((socket.BDADDR_ANY, PSM_CONTROL))
                    s_itr.bind((socket.BDADDR_ANY, PSM_INTERRUPT))
                s_ctrl.listen(1)
                s_itr.listen(1)
                self.bz.set_discoverable(True)
                # The class must be set AFTER discoverability: toggling it resets the class.
                self.bz.set_device_class()
                log.info("waiting for a Switch (open Controllers -> Change Grip/Order)")

                ctrl = itr = None
                first_at = 0.0
                while not stop.is_set():
                    wanted = [s for s, got in ((s_ctrl, ctrl), (s_itr, itr)) if got is None]
                    ready, _, _ = select.select(wanted, [], [], 0.5)
                    self._watchdog()
                    for s in ready:
                        conn, peer = s.accept()
                        if s is s_ctrl:
                            ctrl = (conn, peer)
                        else:
                            itr = (conn, peer)
                        first_at = first_at or time.monotonic()
                    if ctrl and itr:
                        self.bz.set_discoverable(False)
                        return SocketLink(itr[0], ctrl[0], str(itr[1][0]).upper())
                    if first_at and time.monotonic() - first_at > 10:
                        log.info("incomplete connection from the console, retrying")
                        for c in (ctrl, itr):
                            if c:
                                c[0].close()
                        break
            finally:
                s_ctrl.close()
                s_itr.close()
        raise Stopped()

    def connect(self, address: str, stop: threading.Event) -> Link:
        ctrl, itr = _l2cap_socket(), _l2cap_socket()
        try:
            ctrl.settimeout(CONNECT_TIMEOUT)
            itr.settimeout(CONNECT_TIMEOUT)
            ctrl.connect((address, PSM_CONTROL))
            itr.connect((address, PSM_INTERRUPT))
            return SocketLink(itr, ctrl, address.upper())
        except OSError:
            ctrl.close()
            itr.close()
            raise

    # -- housekeeping --------------------------------------------------------------------

    def _watchdog(self) -> None:
        """A Switch that connects and drops twice has a stale pairing; remove it.

        (Same heuristic as nxbt: the console only recovers after the host forgets it.)
        """
        try:
            devs = [d for d in self.bz.devices() if d.alias.upper() == SWITCH_ALIAS.upper()]
        except BlueZError:
            return
        connected = {d.path for d in devs if d.connected}
        for path in self._seen_connected - connected:
            self._watch[path] = self._watch.get(path, 0) + 1
            if self._watch[path] >= 2:
                log.info("console connected and dropped twice; removing stale pairing %s", path)
                try:
                    self.bz.remove_device(path)
                except BlueZError as e:
                    log.debug("remove failed: %s", e)
                self._watch[path] = 0
        self._seen_connected = connected

    def forget(self, address: Optional[str] = None) -> list[str]:
        if self.bz is None:
            self.bz = BlueZ(self.adapter)
        removed = []
        for d in self.bz.devices():
            if (address and d.address == address.upper()) or (not address and d.alias.upper() == SWITCH_ALIAS.upper()):
                try:
                    self.bz.remove_device(d.path)
                    removed.append(d.address)
                except BlueZError as e:
                    log.warning("could not remove %s: %s", d.address, e)
        return removed
