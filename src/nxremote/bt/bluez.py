"""Synchronous facade over the BlueZ D-Bus API (via dbus-fast)."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass
from importlib import resources
from typing import Any, Optional

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
PROFILE_MANAGER_IFACE = "org.bluez.ProfileManager1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OBJMGR_IFACE = "org.freedesktop.DBus.ObjectManager"

PROFILE_PATH = "/nxremote/controller"
PROFILE_UUID = "00001000-0000-1000-8000-00805f9b34fb"

# Class of device 0x002508: peripheral / gamepad
COD_MAJOR, COD_MINOR, COD_HEX = 0x05, 0x08, "0x002508"


class BlueZError(RuntimeError):
    pass


@dataclass(frozen=True)
class BtDevice:
    path: str
    address: str
    alias: str
    connected: bool
    paired: bool


def sdp_record() -> str:
    return resources.files("nxremote.bt").joinpath("sdp/switch-controller.xml").read_text()


class BlueZ:
    """Owns a D-Bus connection running on a private event-loop thread."""

    def __init__(self, adapter: str = "hci0") -> None:
        self.adapter_name = adapter
        self.adapter_path = f"/org/bluez/{adapter}"
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="bluez-dbus", daemon=True)
        self._thread.start()
        self._bus = None
        self._profile_registered = False
        self._call(self._connect())

    # -- plumbing ---------------------------------------------------------------

    def _call(self, coro, timeout: float = 15.0) -> Any:
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except Exception as e:
            if isinstance(e, BlueZError):
                raise
            raise BlueZError(f"{type(e).__name__}: {e}") from e

    async def _connect(self) -> None:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        try:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as e:
            raise BlueZError(f"cannot connect to the system D-Bus: {e}") from e

    async def _iface(self, path: str, iface: str):
        intro = await self._bus.introspect(BLUEZ, path)
        return self._bus.get_proxy_object(BLUEZ, path, intro).get_interface(iface)

    async def _get(self, path: str, iface: str, prop: str):
        props = await self._iface(path, PROPS_IFACE)
        return (await props.call_get(iface, prop)).value

    async def _set(self, path: str, iface: str, prop: str, sig: str, value) -> None:
        from dbus_fast import Variant

        props = await self._iface(path, PROPS_IFACE)
        await props.call_set(iface, prop, Variant(sig, value))

    def close(self) -> None:
        async def _close():
            if self._bus:
                self._bus.disconnect()

        try:
            self._call(_close(), 5)
        except BlueZError:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)

    # -- adapter -----------------------------------------------------------------

    @property
    def address(self) -> str:
        try:
            return str(self._call(self._get(self.adapter_path, ADAPTER_IFACE, "Address"))).upper()
        except BlueZError as e:
            raise BlueZError(
                f"Bluetooth adapter {self.adapter_name!r} not found via BlueZ ({e}). "
                "Is bluetoothd running and the adapter plugged in?"
            ) from e

    def get_adapter_prop(self, prop: str) -> Any:
        return self._call(self._get(self.adapter_path, ADAPTER_IFACE, prop))

    def set_adapter_prop(self, prop: str, sig: str, value) -> None:
        self._call(self._set(self.adapter_path, ADAPTER_IFACE, prop, sig, value))

    def configure(self, alias: str) -> None:
        self.set_adapter_prop("Powered", "b", True)
        self.set_adapter_prop("Pairable", "b", True)
        self.set_adapter_prop("PairableTimeout", "u", 0)
        self.set_adapter_prop("DiscoverableTimeout", "u", 0)
        self.set_adapter_prop("Alias", "s", alias)

    def set_discoverable(self, on: bool) -> None:
        self.set_adapter_prop("Discoverable", "b", on)

    def set_device_class(self) -> bool:
        """Advertise as a gamepad. Must run *after* ``set_discoverable``."""
        idx = self.adapter_name.removeprefix("hci")
        attempts = []
        if shutil.which("hciconfig"):
            attempts.append(["hciconfig", self.adapter_name, "class", COD_HEX])
        if shutil.which("btmgmt"):
            attempts.append(["btmgmt", "--index", idx, "class", str(COD_MAJOR), str(COD_MINOR)])
        for cmd in attempts:
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            except (OSError, subprocess.TimeoutExpired) as e:
                log.debug("%s failed: %s", cmd[0], e)
                continue
            if res.returncode == 0 and not res.stderr.strip():
                return True
            log.debug("%s: %s", " ".join(cmd), (res.stderr or res.stdout).strip())
        log.warning("could not set the Bluetooth device class (need root and hciconfig or btmgmt)")
        return False

    # -- SDP profile ---------------------------------------------------------------

    def register_hid_profile(self) -> None:
        if self._profile_registered:
            return
        self._call(self._register_profile(sdp_record()))
        self._profile_registered = True

    async def _register_profile(self, xml: str) -> None:
        from dbus_fast import DBusError, Variant
        from .profile import Profile

        self._bus.export(PROFILE_PATH, Profile())
        mgr = await self._iface("/org/bluez", PROFILE_MANAGER_IFACE)
        opts = {
            "ServiceRecord": Variant("s", xml),
            "Role": Variant("s", "server"),
            "RequireAuthentication": Variant("b", False),
            "RequireAuthorization": Variant("b", False),
            "AutoConnect": Variant("b", True),
        }
        try:
            await mgr.call_register_profile(PROFILE_PATH, PROFILE_UUID, opts)
        except DBusError as e:
            if "AlreadyExists" not in str(e) and "Already Exists" not in str(e):
                raise

    # -- devices --------------------------------------------------------------------

    def devices(self) -> list[BtDevice]:
        return self._call(self._devices())

    async def _devices(self) -> list[BtDevice]:
        mgr = await self._iface("/", OBJMGR_IFACE)
        objects = await mgr.call_get_managed_objects()
        out = []
        for path, ifaces in objects.items():
            d = ifaces.get(DEVICE_IFACE)
            if not d or not path.startswith(self.adapter_path + "/"):
                continue
            val = lambda k, default=None: d[k].value if k in d else default  # noqa: E731
            out.append(
                BtDevice(
                    path=path,
                    address=str(val("Address", "")).upper(),
                    alias=str(val("Alias", "")),
                    connected=bool(val("Connected", False)),
                    paired=bool(val("Paired", False)),
                )
            )
        return out

    def remove_device(self, path: str) -> None:
        async def _rm():
            adapter = await self._iface(self.adapter_path, ADAPTER_IFACE)
            await adapter.call_remove_device(path)

        self._call(_rm())
