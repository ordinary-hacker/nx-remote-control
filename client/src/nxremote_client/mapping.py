"""Map a standard SDL game controller to Switch button names.

SDL normalises every recognised pad to the "standard" layout whose face buttons
sit in the Xbox arrangement (A bottom, B right, X left, Y top). On a Switch the
same *physical* positions are labelled differently (the bottom button is B, the
right button is A). Two conventions are therefore possible:

* ``positional`` (default): keep the physical position. SDL ``A`` (bottom) ->
  Switch ``B``. This is what a Switch player expects: the bottom button is the
  "confirm/south" button on both.
* ``labels``: match the printed letters. SDL ``A`` -> Switch ``A``. Handy if
  you think in Xbox letters.

Triggers (ZL/ZR) are analog on most pads; a configurable threshold turns them
into the Switch's digital ZL/ZR.
"""

from __future__ import annotations

from dataclasses import dataclass

# SDL_GameControllerButton values (stable API constants).
SDL_A, SDL_B, SDL_X, SDL_Y = 0, 1, 2, 3
SDL_BACK, SDL_GUIDE, SDL_START = 4, 5, 6
SDL_LSTICK, SDL_RSTICK = 7, 8
SDL_LSHOULDER, SDL_RSHOULDER = 9, 10
SDL_DPAD_UP, SDL_DPAD_DOWN, SDL_DPAD_LEFT, SDL_DPAD_RIGHT = 11, 12, 13, 14
SDL_MISC1 = 15  # share/capture/microphone button where present

SDL_AXIS_LX, SDL_AXIS_LY, SDL_AXIS_RX, SDL_AXIS_RY = 0, 1, 2, 3
SDL_AXIS_TRIGGER_L, SDL_AXIS_TRIGGER_R = 4, 5

# Buttons that are the same regardless of face-button convention.
_COMMON = {
    SDL_BACK: "MINUS",
    SDL_START: "PLUS",
    SDL_GUIDE: "HOME",
    SDL_MISC1: "CAPTURE",
    SDL_LSTICK: "L_STICK",
    SDL_RSTICK: "R_STICK",
    SDL_LSHOULDER: "L",
    SDL_RSHOULDER: "R",
    SDL_DPAD_UP: "DPAD_UP",
    SDL_DPAD_DOWN: "DPAD_DOWN",
    SDL_DPAD_LEFT: "DPAD_LEFT",
    SDL_DPAD_RIGHT: "DPAD_RIGHT",
}

_FACE_POSITIONAL = {SDL_A: "B", SDL_B: "A", SDL_X: "Y", SDL_Y: "X"}
_FACE_LABELS = {SDL_A: "A", SDL_B: "B", SDL_X: "X", SDL_Y: "Y"}


@dataclass(frozen=True)
class Mapping:
    face: str = "positional"  # "positional" | "labels"
    trigger_threshold: float = 0.4  # analog trigger value that counts as ZL/ZR
    swap_ab: bool = False
    swap_xy: bool = False

    def button_table(self) -> dict[int, str]:
        face = dict(_FACE_LABELS if self.face == "labels" else _FACE_POSITIONAL)
        if self.swap_ab:
            face[SDL_A], face[SDL_B] = face[SDL_B], face[SDL_A]
        if self.swap_xy:
            face[SDL_X], face[SDL_Y] = face[SDL_Y], face[SDL_X]
        return {**_COMMON, **face}

    def button(self, sdl_button: int) -> str | None:
        return self.button_table().get(sdl_button)
