"""Persistent storage of paired consoles and the API token."""

from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from typing import Optional


def default_state_dir() -> Path:
    env = os.environ.get("NXREMOTE_STATE_DIR")
    if env:
        return Path(env)
    if os.geteuid() == 0:
        return Path("/var/lib/nxremote")
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "nxremote"


class PairingStore:
    """Remembers console Bluetooth addresses (most recent first) for reconnecting."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "pairings.json"
        self._lock = threading.Lock()

    def _read(self) -> list[str]:
        try:
            return [str(a).upper() for a in json.loads(self.path.read_text()).get("switches", [])]
        except (OSError, ValueError, AttributeError):
            return []

    def load(self) -> list[str]:
        with self._lock:
            return self._read()

    def remember(self, address: str) -> None:
        address = address.upper()
        with self._lock:
            self._write([address] + [a for a in self._read() if a != address])

    def forget(self, address: Optional[str] = None) -> list[str]:
        with self._lock:
            current = self._read()
            removed = current if address is None else [a for a in current if a == address.upper()]
            self._write([a for a in current if a not in removed])
            return removed

    def _write(self, switches: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"switches": switches}))
        os.replace(tmp, self.path)


def load_or_create_token(state_dir: Path) -> str:
    """Return the persistent API token, creating a random one on first use."""
    path = state_dir / "token"
    try:
        token = path.read_text().strip()
        if token:
            return token
    except OSError:
        pass
    state_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token
