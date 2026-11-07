"""One-time host setup: stop BlueZ's `input` plugin from owning the HID PSMs (17/19).

BlueZ's input plugin binds L2CAP PSM 17 and 19 for Bluetooth keyboards/mice. We
need those ports to present as a gamepad, so bluetoothd must run with
``--noplugin=input``. This module writes a systemd drop-in for that.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

UNIT = "bluetooth.service"
PERSISTENT_DIR = Path("/etc/systemd/system/bluetooth.service.d")
TEMPORARY_DIR = Path("/run/systemd/system/bluetooth.service.d")
DROPIN = "nxremote.conf"


def parse_exec_start(show_output: str) -> list[str]:
    """Extract argv from ``systemctl show -p ExecStart`` output."""
    m = re.search(r"argv\[\]=(.*?)\s*;\s*ignore_errors", show_output, re.S)
    if not m:
        raise ValueError("could not parse ExecStart from systemctl output")
    return m.group(1).split()


def build_dropin(argv: list[str]) -> str:
    """Systemd drop-in replacing ExecStart with the same command plus ``--noplugin=input``."""
    plugins: list[str] = []
    rest: list[str] = []
    for a in argv:
        if a.startswith("--noplugin="):
            plugins += [p for p in a.split("=", 1)[1].split(",") if p]
        else:
            rest.append(a)
    if "*" not in plugins and "input" not in plugins:
        plugins.append("input")
    return "[Service]\nExecStart=\nExecStart=" + " ".join(rest + ["--noplugin=" + ",".join(plugins)]) + "\n"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _reload() -> None:
    for cmd in (["systemctl", "daemon-reload"], ["systemctl", "restart", UNIT]):
        res = _run(cmd)
        if res.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {res.stderr.strip()}")


def install(temporary: bool = False) -> Path:
    if os.geteuid() != 0:
        raise PermissionError("run as root (sudo)")
    show = _run(["systemctl", "show", UNIT, "-p", "ExecStart"])
    if show.returncode != 0:
        raise RuntimeError(f"systemctl failed: {show.stderr.strip()}")
    argv = parse_exec_start(show.stdout)
    target_dir = TEMPORARY_DIR if temporary else PERSISTENT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / DROPIN
    path.write_text(build_dropin(argv))
    _reload()
    return path


def restore() -> list[Path]:
    if os.geteuid() != 0:
        raise PermissionError("run as root (sudo)")
    removed = []
    for d in (PERSISTENT_DIR, TEMPORARY_DIR):
        p = d / DROPIN
        if p.exists():
            p.unlink()
            removed.append(p)
    _reload()
    return removed


# -- diagnostics ------------------------------------------------------------------------


def doctor(adapter: str = "hci0") -> list[tuple[str, bool, str]]:
    """Return (check, ok, detail) tuples."""
    results: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    has_bt = hasattr(socket, "AF_BLUETOOTH") and hasattr(socket, "BTPROTO_L2CAP")
    add("Python has Bluetooth sockets", has_bt, "" if has_bt else "Python built without Bluetooth support")
    add("running as root", os.geteuid() == 0, "needed to bind L2CAP PSM 17/19 and set the device class")

    try:
        from .bluez import BlueZ

        bz = BlueZ(adapter)
        try:
            add(f"adapter {adapter} reachable via D-Bus", True, bz.address)
        finally:
            bz.close()
    except Exception as e:
        add(f"adapter {adapter} reachable via D-Bus", False, str(e))

    have_tool = bool(shutil.which("hciconfig") or shutil.which("btmgmt"))
    add("hciconfig or btmgmt installed", have_tool, "" if have_tool else "needed to set the gamepad device class")

    if has_bt:
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        try:
            s.bind((socket.BDADDR_ANY, 17))
            add("L2CAP PSM 17 is free (input plugin disabled)", True)
        except OSError as e:
            add(
                "L2CAP PSM 17 is free (input plugin disabled)",
                False,
                f"{e.strerror or e}; run `sudo nxremote setup-bluez`",
            )
        finally:
            s.close()
    return results
