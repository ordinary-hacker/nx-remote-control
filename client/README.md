# nxremote-client

Forward a game controller that's plugged into **your** computer, over the
internet, to an [`nxremote`](../README.md) host — which relays it to a Nintendo
Switch. The Switch's **HD rumble** is sent back and played on your controller.

```
  your gamepad ──USB/BT──► this client ──Internet (WebSocket)──► nxremote host ──BT──► Switch
       ▲                                                                                │
       └──────────────────────────── rumble ◄──────────────────────────────────────────┘
```

So you sit anywhere with your own controller, and play on a Switch that's next
to the Raspberry Pi running `nxremote`.

## Which platforms

| Platform | How | Controller read | Rumble back |
|---|---|---|---|
| **Windows** | `pip install 'nxremote-client[sdl]'` | SDL2 (Xbox, DualSense, Switch Pro, 8BitDo, generic) | yes (XInput/HID pads) |
| **macOS** | `pip install 'nxremote-client[sdl]'` | SDL2 | yes |
| **Linux** | `pip install 'nxremote-client[sdl]'` | SDL2 | yes |
| **Android** | **browser** (recommended) or Termux | Gamepad API / SDL2 | yes / varies |
| **iOS / iPadOS** | **browser** | Gamepad API | best-effort (`navigator.vibrate` only) |

The input layer is pluggable (`InputSource`/`RumbleSink`), and the transport is
pure `websockets`, so the networking half runs anywhere CPython runs.

### Why mobile uses the browser

There is no reliable way for a normal Python process on stock **iOS** or
**Android** to read a Bluetooth/USB game controller — the OSes expose
controllers to apps through native frameworks (GameController.framework,
Android `InputDevice`), not to Python. Rather than ship something that doesn't
work, the honest answer is:

* **Just open the host's built-in web page** in a mobile browser:
  `http://<host>:8000/` (served by `nxremote serve`). It uses the browser
  **Gamepad API** to read a controller paired to the phone/tablet and plays the
  Switch's rumble back through the Gamepad vibration actuator (or
  `navigator.vibrate`). This is the same forwarding this client does, and it
  needs no install. It is the recommended path on iOS and Android.
* On **Android** only, you can also run this Python client under
  [Termux](https://termux.dev) if you have a controller Termux/SDL can see
  (support varies by device); see below.

## Install (desktop)

```bash
pip install 'nxremote-client[sdl]'
```

`pysdl2-dll` bundles the SDL2 native library, so there's nothing else to
install on Windows, macOS, or Linux on x86-64/arm64. On other architectures
install SDL2 from your package manager (e.g. `sudo apt install libsdl2-2.0-0`)
and use `pip install 'nxremote-client[sdl]'` without the bundled binary.

## Use

```bash
# the host prints its token with:  nxremote token
nxremote-client raspberrypi.local:8000 --token YOUR_TOKEN

# list what SDL sees, pick a specific pad, or match printed button letters:
nxremote-client --list
nxremote-client pi:8000 --token T --controller 1
nxremote-client pi:8000 --token T --face labels      # SDL "A" -> Switch A (default keeps physical position)

# no controller handy? drive it from the keyboard (POSIX terminals):
nxremote-client pi:8000 --token T --source keyboard
```

Host can be `HOST`, `HOST:PORT` (default port 8000), or a full
`ws://`/`wss://`/`http://`/`https://` URL. For TLS behind a reverse proxy, pass
the full URL with the right port (e.g. `wss://play.example.com:443`).
`$NXREMOTE_HOST` and `$NXREMOTE_TOKEN` are read if the flags are omitted.

Useful flags: `--no-rumble`, `--deadzone 0.1`, `--trigger-threshold 0.5`,
`--swap-ab`, `--swap-xy`, `--rate 120`, `--no-reconnect`, `--keep-on-exit`
(by default the host releases all inputs when the client disconnects — a
dead-man's switch).

### Button mapping

SDL reports every pad in the "standard" (Xbox) layout. By default
(`--face positional`) the client preserves the **physical** position: the
bottom face button becomes Switch **B**, the right one **A** — what a Switch
player expects. Use `--face labels` to match the printed letters instead.
Triggers become the digital **ZL/ZR** past `--trigger-threshold`. The stick
Y-axis is flipped to the host's "+y is up" convention.

## Run from source / as a module

```bash
cd client
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m nxremote_client pi:8000 --token T
.venv/bin/pytest          # unit tests + end-to-end against an in-process host (if nxremote is installed)
```

## Ship a single executable

The client is pure Python + SDL2, so it freezes cleanly:

```bash
pip install pyinstaller
pyinstaller --onefile --name nxremote-client -c src/nxremote_client/__main__.py \
            --collect-binaries sdl2dll
# dist/nxremote-client  (hand it to a friend who has no Python)
```

## Android via Termux (advanced, optional)

```bash
pkg install python
pip install nxremote-client          # transport only, no SDL
# controller input through SDL on Termux is device-dependent; if your setup
# can't read the pad, use the browser page at http://<host>:8000/ instead.
nxremote-client <host>:8000 --token T --source keyboard   # works without a pad
```

## How it works

* `transport.py` — async WebSocket client (`websockets`): sends
  `{"type":"input", ...}` only when the state changes (plus a ~1 Hz keep-alive),
  receives `rumble`/`state` events, reconnects on drop. No device dependencies.
* `sources.py` / `sdl_backend.py` / `keyboard.py` — `InputSource`s that produce
  a `ControllerState`, and `RumbleSink`s that play rumble.
* `mapping.py` — SDL standard layout → Switch button names.
* `runner.py` — polls the source (off-thread, since SDL calls block), diffs,
  forwards, and feeds rumble back to the sink.

## License

MIT (same as the host). See [../NOTICE](../NOTICE).
