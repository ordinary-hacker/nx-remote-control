"""Switch Pro Controller protocol: parse host (output) reports, build input reports.

This class is transport agnostic. Feed it every packet received on the HID
interrupt channel with :meth:`handle_output` and send whatever
:meth:`build_input_report` / the returned replies produce.
"""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import rumble as rumble_mod
from .imu import encode_report_block
from .spi import build_flash
from .state import BUTTONS, NEUTRAL, ControllerState
from .sticks import LEFT_CAL, RIGHT_CAL, encode_stick, stick_to_u16

log = logging.getLogger(__name__)

HIDP_INPUT = 0xA1  # DATA | INPUT  (controller -> host)
HIDP_OUTPUT = 0xA2  # DATA | OUTPUT (host -> controller)

REPORT_SUBCMD_REPLY = 0x21
REPORT_STANDARD = 0x30
REPORT_NFC_IR = 0x31
REPORT_SIMPLE = 0x3F

RUMBLE_REPORT_IDS = (0x01, 0x10, 0x11, 0x12)

MODE_NAMES = {0x30: "standard", 0x31: "nfc_ir", 0x3F: "simple"}

DEFAULT_BODY_COLOUR = bytes((0x32, 0x32, 0x32))
DEFAULT_BUTTON_COLOUR = bytes((0xFF, 0xFF, 0xFF))

# Emit the same 'vibrator input report' bytes real controllers produce.
_VIBRATOR_BYTES = (0xA0, 0xB0, 0xC0, 0x90)

Event = Callable[[str, dict], None]


@dataclass
class PlayerLights:
    raw: int = 0

    @property
    def solid(self) -> list[bool]:
        return [bool(self.raw & (1 << i)) for i in range(4)]

    @property
    def flashing(self) -> list[bool]:
        return [bool(self.raw & (0x10 << i)) for i in range(4)]

    @property
    def player_number(self) -> Optional[int]:
        low = self.raw & 0x0F
        if low in (0x01, 0x03, 0x07, 0x0F):
            return {0x01: 1, 0x03: 2, 0x07: 3, 0x0F: 4}[low]
        high = (self.raw >> 4) & 0x0F
        if high in (0x01, 0x03, 0x07, 0x0F):
            return {0x01: 1, 0x03: 2, 0x07: 3, 0x0F: 4}[high]
        return None

    def to_dict(self) -> dict:
        return {"raw": self.raw, "solid": self.solid, "flashing": self.flashing, "player": self.player_number}


class ProControllerProtocol:
    CONTROLLER_ID = 0x03  # Pro Controller
    FIRMWARE = (0x03, 0x8B)

    def __init__(
        self,
        bt_address: str,
        *,
        body_colour: bytes = DEFAULT_BODY_COLOUR,
        button_colour: bytes = DEFAULT_BUTTON_COLOUR,
        on_event: Optional[Event] = None,
    ) -> None:
        self.bt_address = bt_address
        self.on_event: Event = on_event or (lambda name, payload: None)
        self.flash = build_flash(bytes(body_colour), bytes(button_colour))
        self._t0 = time.perf_counter()
        self._vib_idx = 0

        # Host-controlled settings
        self.mode = 0x3F  # A fresh controller starts in simple HID mode.
        self.device_info_queried = False
        self.imu_enabled = False
        self.vibration_enabled = False
        self.player_lights = PlayerLights()
        self.player_lights_set = False
        self.home_light: bytes = b""
        self.disconnect_requested = False
        self.last_rumble: tuple[bytes, bytes] = (rumble_mod.NEUTRAL_RUMBLE, rumble_mod.NEUTRAL_RUMBLE)
        self.last_subcommand_at = time.monotonic()
        self.subcommands_seen = 0

    # -- helpers ---------------------------------------------------------

    @property
    def mode_name(self) -> str:
        return MODE_NAMES.get(self.mode, hex(self.mode))

    @property
    def handshake_complete(self) -> bool:
        """The console finished its initial configuration of the controller."""
        return self.vibration_enabled and self.player_lights_set

    def _timer(self) -> int:
        return int((time.perf_counter() - self._t0) * 200) & 0xFF

    def _effective(self, state: ControllerState) -> ControllerState:
        # Until the console has queried device info it must see an idle controller.
        return state if self.device_info_queried else NEUTRAL.with_(battery=state.battery)

    @staticmethod
    def _button_bytes(state: ControllerState) -> bytes:
        out = [0, 0, 0]
        for name in state.buttons:
            idx, mask = BUTTONS[name]
            out[idx] |= mask
        return bytes(out)

    def _common(self, report_id: int, state: ControllerState) -> bytearray:
        s = self._effective(state)
        buf = bytearray(50)
        buf[0] = HIDP_INPUT
        buf[1] = report_id
        buf[2] = self._timer()
        buf[3] = s.battery.nibble() | 0x00  # connection info: 0 = Pro Controller/Switch
        buf[4:7] = self._button_bytes(s)
        buf[7:10] = encode_stick(LEFT_CAL, s.left_stick)
        buf[10:13] = encode_stick(RIGHT_CAL, s.right_stick)
        buf[13] = _VIBRATOR_BYTES[self._vib_idx]
        return buf

    # -- input reports -----------------------------------------------------

    def build_input_report(self, state: ControllerState) -> bytes:
        """Build the periodic input report matching the negotiated mode."""
        if self.mode == REPORT_SIMPLE:
            return self._simple_report(self._effective(state))
        buf = self._common(REPORT_STANDARD, state)
        if self.imu_enabled:
            buf[14:50] = encode_report_block(self._effective(state).imu)
        return bytes(buf)

    def _simple_report(self, s: ControllerState) -> bytes:
        b = s.buttons
        b0 = (
            (0x01 if "B" in b else 0)
            | (0x02 if "A" in b else 0)
            | (0x04 if "Y" in b else 0)
            | (0x08 if "X" in b else 0)
            | (0x10 if "L" in b else 0)
            | (0x20 if "R" in b else 0)
            | (0x40 if "ZL" in b else 0)
            | (0x80 if "ZR" in b else 0)
        )
        b1 = (
            (0x01 if "MINUS" in b else 0)
            | (0x02 if "PLUS" in b else 0)
            | (0x04 if "L_STICK" in b else 0)
            | (0x08 if "R_STICK" in b else 0)
            | (0x10 if "HOME" in b else 0)
            | (0x20 if "CAPTURE" in b else 0)
        )
        up, down, left, right = (f"DPAD_{d}" in b for d in ("UP", "DOWN", "LEFT", "RIGHT"))
        hat_table = {
            (True, False, False, False): 0,
            (True, False, False, True): 1,
            (False, False, False, True): 2,
            (False, True, False, True): 3,
            (False, True, False, False): 4,
            (False, True, True, False): 5,
            (False, False, True, False): 6,
            (True, False, True, False): 7,
        }
        hat = hat_table.get((up, down, left, right), 8)
        lx, ly = stick_to_u16(s.left_stick)
        rx, ry = stick_to_u16(s.right_stick)
        # The simple report's Y axis grows downwards.
        return bytes((HIDP_INPUT, REPORT_SIMPLE, b0, b1, hat)) + struct.pack(
            "<4H", lx, 0xFFFF - ly, rx, 0xFFFF - ry
        )

    # -- host (output) reports --------------------------------------------

    def handle_output(self, data: bytes, state: ControllerState) -> Optional[bytes]:
        """Process a packet from the console. Returns a reply packet or None."""
        if not data or data[0] != HIDP_OUTPUT or len(data) < 3:
            return None
        report_id = data[1]

        if report_id in RUMBLE_REPORT_IDS and len(data) >= 11:
            self._handle_rumble(data[3:7], data[7:11])

        if report_id != 0x01 or len(data) < 12:
            return None
        sub = data[11]
        args = bytes(data[12:])
        self.last_subcommand_at = time.monotonic()
        self.subcommands_seen += 1
        return self._handle_subcommand(sub, args, state)

    def _handle_rumble(self, left: bytes, right: bytes) -> None:
        pair = (bytes(left), bytes(right))
        if pair == self.last_rumble:
            return
        self.last_rumble = pair
        if not self.vibration_enabled:
            return
        self.on_event(
            "rumble",
            {"left": rumble_mod.decode(pair[0]).to_dict(), "right": rumble_mod.decode(pair[1]).to_dict()},
        )

    def _reply(self, state: ControllerState, ack: int, sub: int, data: bytes = b"") -> bytes:
        buf = self._common(REPORT_SUBCMD_REPLY, state)
        self._vib_idx = (self._vib_idx + 1) % len(_VIBRATOR_BYTES)
        buf[14] = ack
        buf[15] = sub
        payload = data[: 50 - 16]
        buf[16 : 16 + len(payload)] = payload
        return bytes(buf)

    def _handle_subcommand(self, sub: int, args: bytes, state: ControllerState) -> Optional[bytes]:
        r = lambda ack, data=b"": self._reply(state, ack, sub, data)  # noqa: E731

        if sub == 0x00:  # get only controller state
            return r(0x80)
        if sub == 0x02:  # request device info
            self.device_info_queried = True
            mac = bytes(int(p, 16) for p in self.bt_address.split(":"))
            return r(0x82, bytes(self.FIRMWARE) + bytes((self.CONTROLLER_ID, 0x02)) + mac + bytes((0x01, 0x01)))
        if sub == 0x03:  # set input report mode
            mode = args[0] if args else 0x30
            if mode != self.mode:
                self.mode = mode
                self.on_event("mode", {"mode": self.mode_name})
            return r(0x80)
        if sub == 0x04:  # trigger buttons elapsed time
            return r(0x83, bytes(14))
        if sub == 0x06:  # set HCI state (disconnect / reboot)
            self.disconnect_requested = True
            return r(0x80)
        if sub == 0x08:  # set shipment low power state
            return r(0x80)
        if sub == 0x10:  # SPI flash read
            if len(args) < 5:
                return None
            addr = struct.unpack_from("<I", args, 0)[0]
            length = min(args[4], 0x1D)
            chunk = self._flash_read(addr, length)
            return r(0x90, args[:5] + chunk)
        if sub == 0x11:  # SPI flash write
            if len(args) >= 5:
                addr = struct.unpack_from("<I", args, 0)[0]
                payload = args[5 : 5 + args[4]]
                if addr + len(payload) <= len(self.flash):
                    self.flash[addr : addr + len(payload)] = payload
            return r(0x80, b"\x00")
        if sub == 0x20:  # reset NFC/IR MCU
            return r(0x80)
        if sub == 0x21:  # set NFC/IR MCU configuration
            data = bytes((0x01, 0x00, 0xFF, 0x00, 0x08, 0x00, 0x1B, 0x01)) + bytes(25) + b"\xc8"
            return r(0xA0, data)
        if sub == 0x22:  # set NFC/IR MCU state
            return r(0x80)
        if sub == 0x30:  # set player lights
            self.player_lights = PlayerLights(args[0] if args else 0)
            self.player_lights_set = True
            self.on_event("player_lights", self.player_lights.to_dict())
            return r(0x80)
        if sub == 0x31:  # get player lights
            return r(0xB0, bytes((self.player_lights.raw,)))
        if sub == 0x38:  # set HOME light
            self.home_light = args
            self.on_event("home_light", {"pattern": args.hex()})
            return r(0x80)
        if sub == 0x40:  # enable / disable IMU
            enabled = bool(args and args[0] == 0x01)
            if enabled != self.imu_enabled:
                self.imu_enabled = enabled
                self.on_event("imu_enabled", {"enabled": enabled})
            return r(0x80)
        if sub == 0x41:  # IMU sensitivity
            return r(0x80)
        if sub == 0x42:  # write IMU register
            return r(0x80)
        if sub == 0x43:  # read IMU register
            return r(0xC0, args[:2] + bytes(args[1] if len(args) > 1 else 0))
        if sub == 0x48:  # enable / disable vibration
            enabled = bool(args and args[0] == 0x01)
            if enabled != self.vibration_enabled:
                self.vibration_enabled = enabled
                self.on_event("vibration_enabled", {"enabled": enabled})
            return r(0x82)
        if sub == 0x50:  # battery voltage
            return r(0xD0, struct.pack("<H", 1680) + bytes((0x00, 0x00)))
        log.debug("ignoring unsupported subcommand 0x%02x args=%s", sub, args.hex())
        return None

    def _flash_read(self, addr: int, length: int) -> bytes:
        if addr >= len(self.flash):
            return b"\xff" * length
        chunk = bytes(self.flash[addr : addr + length])
        return chunk + b"\xff" * (length - len(chunk))
