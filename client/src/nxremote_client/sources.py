"""Input source and rumble sink abstractions.

A *source* produces :class:`ControllerState` snapshots from some local device.
A *sink* plays rumble back on that device. Concrete implementations (SDL,
keyboard, fakes for tests) live in sibling modules. Neither the transport nor
the runner import SDL directly, so the whole stack can be unit-tested and the
non-SDL paths work with only ``websockets`` installed.
"""

from __future__ import annotations

import abc

from .state import NEUTRAL, ControllerState, RumbleState


class InputSource(abc.ABC):
    """Reads a local controller and reports its state."""

    #: Human-readable description of the connected device, filled after open().
    device_name: str = "none"

    def open(self) -> None:
        """Acquire the device. May raise if nothing suitable is present."""

    @abc.abstractmethod
    def poll(self) -> ControllerState:
        """Return the current controller state (called at the poll rate)."""

    def close(self) -> None:
        pass

    def __enter__(self) -> "InputSource":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class RumbleSink(abc.ABC):
    """Plays rumble on the local device."""

    @abc.abstractmethod
    def rumble(self, state: RumbleState) -> None:
        ...

    def close(self) -> None:
        pass


class NullSink(RumbleSink):
    """Does nothing (device has no rumble, or rumble is disabled)."""

    def rumble(self, state: RumbleState) -> None:
        pass


class StaticSource(InputSource):
    """A source whose state is set programmatically. Useful for tests and as a
    building block for non-SDL input (e.g. a GUI or a network bridge)."""

    def __init__(self, name: str = "static") -> None:
        self.device_name = name
        self._state = NEUTRAL

    def set_state(self, state: ControllerState) -> None:
        self._state = state

    def poll(self) -> ControllerState:
        return self._state
