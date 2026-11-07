# NOTE: deliberately no `from __future__ import annotations` here: dbus-fast reads
# D-Bus type signatures from the (string) annotations at runtime.
from dbus_fast.service import ServiceInterface, method


class Profile(ServiceInterface):
    """Minimal org.bluez.Profile1 so BlueZ has an object to talk to.

    The HID service itself is served over raw L2CAP sockets; this only exists
    to publish the SDP record through ProfileManager1.RegisterProfile.
    """

    def __init__(self):
        super().__init__("org.bluez.Profile1")

    @method()
    def Release(self):  # noqa: N802
        pass

    @method()
    def NewConnection(self, device: "o", fd: "h", fd_properties: "a{sv}"):  # noqa: N802,F821
        pass

    @method()
    def RequestDisconnection(self, device: "o"):  # noqa: N802,F821
        pass
