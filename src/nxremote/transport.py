"""Transport abstractions between the controller core and a (real or virtual) console."""

from __future__ import annotations

import abc
import select
import socket
import threading
from typing import Optional, Sequence


class LinkClosed(Exception):
    """The console closed the connection."""


class Stopped(Exception):
    """A blocking connect/listen was interrupted by a stop request."""


class Link(abc.ABC):
    """An established HID connection (interrupt channel + control channel)."""

    peer: str = ""

    @abc.abstractmethod
    def fileno_list(self) -> Sequence[int]:
        """File descriptors to wait on for incoming data."""

    @abc.abstractmethod
    def recv(self) -> Optional[bytes]:
        """Return one pending packet from the console, or None. Raises LinkClosed."""

    @abc.abstractmethod
    def send(self, data: bytes) -> None:
        """Send one packet (dropped silently if the socket buffer is full)."""

    @abc.abstractmethod
    def close(self) -> None: ...


class Connector(abc.ABC):
    """Creates links: waits for a console to connect, or connects to a known one."""

    address: str = "00:00:00:00:00:00"

    def prepare(self) -> None:
        """One-time adapter setup."""

    @abc.abstractmethod
    def listen(self, stop: threading.Event) -> Link:
        """Become discoverable and wait for a console. Raises Stopped."""

    @abc.abstractmethod
    def connect(self, address: str, stop: threading.Event) -> Link:
        """Connect to a previously paired console. Raises OSError on failure."""

    def cleanup(self) -> None:
        """Undo adapter changes."""

    def forget(self, address: Optional[str] = None) -> list[str]:
        """Remove pairing information. Returns removed addresses."""
        return []


class SocketLink(Link):
    """Link over two SEQPACKET sockets (L2CAP for Bluetooth, AF_UNIX for virtual)."""

    def __init__(self, itr: socket.socket, ctrl: socket.socket, peer: str) -> None:
        self.itr = itr
        self.ctrl = ctrl
        self.peer = peer
        self.itr.setblocking(False)
        self.ctrl.setblocking(False)
        self._closed = False

    def fileno_list(self) -> Sequence[int]:
        return [self.itr.fileno(), self.ctrl.fileno()]

    def recv(self) -> Optional[bytes]:
        if self._closed:
            raise LinkClosed()
        # Drain the control channel (HIDP handshakes); only liveness matters.
        try:
            while True:
                if self.ctrl.recv(256) == b"":
                    raise LinkClosed()
        except BlockingIOError:
            pass
        except OSError as e:
            raise LinkClosed() from e
        try:
            data = self.itr.recv(512)
        except BlockingIOError:
            return None
        except OSError as e:
            raise LinkClosed() from e
        if data == b"":
            raise LinkClosed()
        return data

    def send(self, data: bytes) -> None:
        if self._closed:
            raise LinkClosed()
        try:
            self.itr.send(data)
        except BlockingIOError:
            pass
        except OSError as e:
            raise LinkClosed() from e

    def close(self) -> None:
        self._closed = True
        for s in (self.itr, self.ctrl):
            try:
                s.close()
            except OSError:
                pass


def wait_readable(fds: Sequence[int], timeout: float) -> None:
    if timeout < 0:
        timeout = 0
    try:
        select.select(list(fds), [], [], timeout)
    except (OSError, ValueError):
        pass
