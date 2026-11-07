"""Request/response models of the HTTP + WebSocket API."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..hid.state import (
    BATTERY_LEVELS,
    Battery,
    Imu,
    Stick,
    normalize_buttons,
)
from ..macros import Step, make_overlay


class StickModel(BaseModel):
    """Stick position. x: -1 (left) .. 1 (right); y: -1 (down) .. 1 (up)."""

    model_config = ConfigDict(extra="forbid")
    x: float = Field(0.0, ge=-1.0, le=1.0)
    y: float = Field(0.0, ge=-1.0, le=1.0)

    def to_stick(self) -> Stick:
        return Stick(self.x, self.y)


class ImuModel(BaseModel):
    """Six-axis sensor values (what the console's motion controls read)."""

    model_config = ConfigDict(extra="forbid")
    accel: tuple[float, float, float] = Field((0.0, 0.0, 1.0), description="g; flat & still is [0, 0, 1]")
    gyro: tuple[float, float, float] = Field((0.0, 0.0, 0.0), description="degrees per second")

    @field_validator("accel")
    @classmethod
    def _accel(cls, v):
        if any(abs(a) > 8.0 for a in v):
            raise ValueError("accelerometer range is +-8 g")
        return v

    @field_validator("gyro")
    @classmethod
    def _gyro(cls, v):
        if any(abs(g) > 2000.0 for g in v):
            raise ValueError("gyroscope range is +-2000 deg/s")
        return v

    def to_imu(self) -> Imu:
        return Imu(tuple(self.accel), tuple(self.gyro))


class BatteryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: str = "full"
    charging: bool = False

    @field_validator("level")
    @classmethod
    def _level(cls, v):
        if v not in BATTERY_LEVELS:
            raise ValueError(f"level must be one of {', '.join(BATTERY_LEVELS)}")
        return v

    def to_battery(self) -> Battery:
        return Battery(self.level, self.charging)


class InputPatch(BaseModel):
    """Partial update of the direct input state; omitted fields are untouched."""

    model_config = ConfigDict(extra="forbid")
    buttons: Optional[list[str]] = Field(None, description="Replace the set of held buttons")
    press: list[str] = Field(default_factory=list, description="Additionally hold these buttons")
    release: list[str] = Field(default_factory=list, description="Release these buttons")
    left_stick: Optional[StickModel] = None
    right_stick: Optional[StickModel] = None
    imu: Optional[ImuModel] = None
    battery: Optional[BatteryModel] = None

    @field_validator("buttons", "press", "release")
    @classmethod
    def _buttons(cls, v):
        if v is not None:
            normalize_buttons(v)  # raises ValueError on unknown names
        return v


class StepModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    buttons: list[str] = Field(default_factory=list)
    left_stick: Optional[StickModel] = None
    right_stick: Optional[StickModel] = None
    imu: Optional[ImuModel] = None
    duration_ms: float = Field(100.0, ge=0, le=600_000)

    @field_validator("buttons")
    @classmethod
    def _buttons(cls, v):
        normalize_buttons(v)
        return v

    def to_step(self) -> Step:
        return Step(
            self.duration_ms / 1000.0,
            make_overlay(
                normalize_buttons(self.buttons),
                self.left_stick.to_stick() if self.left_stick else None,
                self.right_stick.to_stick() if self.right_stick else None,
                self.imu.to_imu() if self.imu else None,
            ),
        )


class MacroRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[StepModel] = Field(min_length=1, max_length=10_000)
    repeat: int = Field(1, ge=1, le=100_000)
    wait: bool = Field(False, description="Respond only after the macro finished")


class TapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    buttons: list[str] = Field(min_length=1)
    hold_ms: float = Field(80.0, ge=10, le=60_000)
    release_ms: float = Field(50.0, ge=0, le=60_000, description="Pause after the tap so repeated taps register")
    repeat: int = Field(1, ge=1, le=10_000)
    wait: bool = False

    @field_validator("buttons")
    @classmethod
    def _buttons(cls, v):
        normalize_buttons(v)
        return v


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reconnect: bool = Field(True, description="First try to reconnect to a console paired before")
    address: Optional[str] = Field(None, pattern=r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
