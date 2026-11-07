"""HD rumble (linear resonant actuator) data decoding and encoding.

Each actuator receives 4 bytes: a high band (frequency + amplitude) and a low
band (frequency + amplitude). See dekuNukem/Nintendo_Switch_Reverse_Engineering
(rumble_data_table.md) for the reference tables this follows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

NEUTRAL_RUMBLE = bytes((0x00, 0x01, 0x40, 0x40))


def _amp_from_code(code: int) -> float:
    """code is the 'encoded amplitude' (HA byte / 2), 0..100 is the safe range."""
    if code <= 0:
        return 0.0
    if code == 1:
        return 0.007843
    if code < 16:
        return 0.1125 * 2 ** ((code - 15) / 4)
    if code < 32:
        return 2 ** (code / 16) / 17
    return 2 ** (code / 32) / 8.7


def _code_from_amp(amp: float) -> int:
    if amp <= 0.0:
        return 0
    amp = min(amp, 1.003)
    if amp >= 0.2299:
        return int(round(math.log2(amp * 8.7) * 32))
    if amp >= 0.1176:
        return int(round(math.log2(amp * 17) * 16))
    if amp >= 0.1125 * 2 ** (-13 / 4):
        return max(2, int(round(math.log2(amp / 0.1125) * 4 + 15)))
    return 1


@dataclass(frozen=True)
class RumbleBand:
    frequency: float  # Hz
    amplitude: float  # 0.0 (off) .. ~1.0 (full strength)


@dataclass(frozen=True)
class Rumble:
    """Decoded state of one actuator."""

    high: RumbleBand
    low: RumbleBand
    raw: bytes

    @property
    def intensity(self) -> float:
        return min(1.0, max(self.high.amplitude, self.low.amplitude))

    @property
    def active(self) -> bool:
        return self.intensity > 0.0

    def to_dict(self) -> dict:
        return {
            "high": {"frequency": round(self.high.frequency, 2), "amplitude": round(self.high.amplitude, 4)},
            "low": {"frequency": round(self.low.frequency, 2), "amplitude": round(self.low.amplitude, 4)},
            "intensity": round(self.intensity, 4),
            "active": self.active,
            "raw": self.raw.hex(),
        }


def decode(data: bytes) -> Rumble:
    if len(data) != 4:
        raise ValueError("rumble data must be 4 bytes")
    b0, b1, b2, b3 = data
    hf_raw = b0 | ((b1 & 0x01) << 8)
    hf_enc = hf_raw // 4 + 0x60
    hf_freq = 10.0 * 2 ** (hf_enc / 32)
    hf_amp = _amp_from_code((b1 & 0xFE) >> 1)

    lf_enc = (b2 & 0x7F) + 0x40
    lf_freq = 10.0 * 2 ** (lf_enc / 32)
    lf_code = 0 if b3 < 0x40 else (b3 - 0x40) * 2 + (b2 >> 7)
    lf_amp = _amp_from_code(lf_code)
    return Rumble(RumbleBand(hf_freq, hf_amp), RumbleBand(lf_freq, lf_amp), bytes(data))


def encode(hf_freq: float = 320.0, hf_amp: float = 0.0, lf_freq: float = 160.0, lf_amp: float = 0.0) -> bytes:
    """Encode a rumble command (used by the virtual Switch and tests)."""

    def freq_code(f: float) -> int:
        f = min(max(f, 41.0), 1252.0)
        return int(round(math.log2(f / 10.0) * 32))

    hf_enc = min(max(freq_code(hf_freq), 0x60), 0x60 + 0x7F)
    hf = (hf_enc - 0x60) * 4
    lf = min(max(freq_code(lf_freq) - 0x40, 0), 0x7F)
    hf_code = _code_from_amp(hf_amp)
    lf_code = _code_from_amp(lf_amp)
    hf_amp_b = hf_code * 2
    lf_amp16 = (lf_code // 2 + 0x40) | (0x8000 if lf_code & 1 else 0)
    return bytes(
        (
            hf & 0xFF,
            (hf_amp_b + ((hf >> 8) & 0xFF)) & 0xFF,
            (lf + ((lf_amp16 >> 8) & 0xFF)) & 0xFF,
            lf_amp16 & 0xFF,
        )
    )
