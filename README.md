# nxremote

Turn a Raspberry Pi (or any Linux machine with Bluetooth) into a **Nintendo Switch Pro Controller that you drive over the network**.
The Switch sees a normal Pro Controller; you send inputs through an HTTP/WebSocket API and get **HD rumble** (and the other host-to-controller signals) streamed back.

```
 your app / script / browser ──HTTP + WebSocket──►  Raspberry Pi  ──Bluetooth HID──►  Nintendo Switch
        ▲                                         (nxremote)                            │
        └──────── rumble, player LEDs, status ◄───────────────────────────────────────────┘
```

Prior art: [NXBT](https://github.com/Brikwerk/nxbt) and [dekuNukem's protocol notes](https://github.com/dekuNukem/Nintendo_Switch_Reverse_Engineering). This is an independent implementation with a typed protocol layer, a rumble channel, a macro engine, authentication and a simulator for testing without hardware (see [NOTICE](NOTICE)).

## What it emulates

| Pro Controller feature | Status |
|---|---|
| 14 buttons, D-pad, L/R/ZL/ZR, stick clicks, +/−, Home, Capture | yes |
| Two analog sticks (12-bit, factory-style calibration data served over the SPI-flash emulation) | yes |
| **HD rumble**, both actuators, high + low band frequency/amplitude decoded and streamed | yes |
| 6-axis IMU (accelerometer + gyroscope), host enable/disable, 3 samples per report, calibration data | yes |
| Player LEDs and HOME-button light (reported to your API) | yes |
| Battery level / charging state (settable) | yes |
| Initial pairing ("Change Grip/Order"), reconnection (outbound connect, as a real controller does to wake a console), stale-pairing cleanup | yes (needs hardware validation, see limitations) |
| Simple HID (0x3F) and standard (0x30) report modes, subcommands 0x00–0x50 used by the console | yes |
| Colours (body/buttons) shown on the console | yes |
| NFC (amiibo) / IR camera | **no** – the MCU commands are acknowledged but no tag/camera data is produced |

## Requirements

* Raspberry Pi 3/4/5/Zero 2 W (built-in Bluetooth works) or another Linux box with a BlueZ-supported adapter. Many cheap USB dongles do **not** work as HID peripherals; if connecting fails, try another adapter.
* Raspberry Pi OS / Debian / Ubuntu with Python ≥ 3.10 **from the distro** (pyenv/conda builds often lack Bluetooth sockets – `nxremote doctor` tells you).
* Root (binds L2CAP PSM 17/19 and configures the adapter).

## Install on a Raspberry Pi

```bash
git clone <this repo> && cd nx-remote-control
sudo ./deploy/install.sh        # venv in /opt/nxremote, BlueZ tweak, systemd service on :8000
sudo /opt/nxremote/venv/bin/nxremote doctor
```

The installer runs `nxremote setup-bluez`, which restarts `bluetoothd` with `--noplugin=input` (BlueZ's input plugin otherwise owns the HID ports the Switch connects to). Side effect: Bluetooth keyboards/mice stop working on that host. Undo with `nxremote setup-bluez --restore`.

Manual run (development):

```bash
python3 -m venv .venv --system-site-packages && .venv/bin/pip install -e '.[dev]'
sudo .venv/bin/nxremote setup-bluez
sudo .venv/bin/nxremote serve --host 0.0.0.0
```

The API token is generated on first start and stored in the state dir (`/var/lib/nxremote/token` as root); show it with `nxremote token`. Use `--token`/`NXREMOTE_TOKEN` to set your own, or `--no-auth` on a trusted network only.

## First connection

1. On the Switch: **Controllers → Change Grip/Order** (stay on that screen).
2. Start the service (or `POST /v1/connection/start`). State goes `waiting` → `pairing` → `connected` (watch `GET /v1/status`).
3. After pairing, the Pi is remembered. Next time it reconnects by itself, waking a sleeping Switch like a real controller. If the console shows the controller but ignores input, tap a button once (`POST /v1/tap {"buttons":["a"]}`).

If pairing misbehaves: forget the controller on the Switch, `DELETE /v1/pairings` (or `nxremote forget`), and retry.

## Play with your own controller, from anywhere

[`client/`](client/) is a separate cross-platform Python app (`nxremote-client`) that reads a gamepad plugged into **your** machine and forwards it to the host over the internet, playing the Switch's rumble back on your pad. Works on Windows, macOS and Linux via SDL2; on iOS/Android use the host's built-in web page at `http://<host>:8000/` instead (browser Gamepad API). See [client/README.md](client/README.md).

```bash
pip install 'nxremote-client[sdl]'
nxremote-client <host>:8000 --token "$(nxremote token)"
```

## API

Interactive docs: `http://<pi>:8000/docs`. Built-in web UI (on-screen controller, **forwards a physical gamepad and plays the Switch's rumble back on it**): `http://<pi>:8000/`.

All `/v1` endpoints need `Authorization: Bearer <token>` (also accepted: `X-API-Key`, or `?token=`).

| Endpoint | Purpose |
|---|---|
| `GET /v1/status` | connection state, negotiated mode, IMU/vibration flags, player LEDs, last rumble, current input |
| `POST /v1/connection/start` `{reconnect, address}` / `stop` / `restart` | control the Bluetooth session |
| `DELETE /v1/pairings[?address=]` | forget consoles |
| `GET /v1/input` | direct (API-set) and effective (incl. running macros) state |
| `PATCH /v1/input` | partial update: `buttons` (replace), `press`, `release`, `left_stick`, `right_stick`, `imu`, `battery` |
| `PUT /v1/input` | replace the whole state (unspecified → neutral) |
| `POST /v1/input/release-all` | neutral everything and cancel macros |
| `POST /v1/tap` | `{buttons, hold_ms=80, release_ms=50, repeat=1, wait=false}` |
| `POST /v1/macros` | `{steps:[{buttons, left_stick, right_stick, imu, duration_ms}], repeat, wait}` – timed sequences, run FIFO on the Pi |
| `GET/DELETE /v1/macros[/{id}]` | inspect / cancel |
| `GET /v1/rumble` | last rumble command from the console |
| `WS /v1/ws` | low-latency streaming + events |

Buttons: `A B X Y L R ZL ZR PLUS MINUS HOME CAPTURE L_STICK R_STICK DPAD_UP DPAD_DOWN DPAD_LEFT DPAD_RIGHT` (case-insensitive; aliases such as `up`, `ls`, `start`).
Sticks: `x`,`y` in `[-1, 1]` (+x right, +y up). IMU: `accel` in g (flat and still = `[0,0,1]`, ±8 g), `gyro` in °/s (±2000).

```bash
T=...; H="Authorization: Bearer $T"
curl -H "$H" -X PATCH localhost:8000/v1/input -H 'content-type: application/json' \
     -d '{"press":["ZR"],"left_stick":{"x":-1,"y":0}}'
curl -H "$H" -X POST  localhost:8000/v1/tap -H 'content-type: application/json' \
     -d '{"buttons":["a"],"hold_ms":120,"wait":true}'
curl -H "$H" -X POST  localhost:8000/v1/macros -H 'content-type: application/json' -d '{
  "steps":[{"buttons":["DPAD_DOWN"],"duration_ms":80},{"duration_ms":80}], "repeat":3, "wait":true}'
```

Timing note: the console polls at ~66 Hz, so holds shorter than ~30 ms may be missed. Macros are timed on the Pi, which avoids network jitter; prefer them over client-side sleeps.

### WebSocket

`ws://<pi>:8000/v1/ws?token=…` (options: `events=rumble,state` to filter, `release_on_close=false` to keep inputs held when the socket closes; by default a socket that sent input releases everything when it disconnects – a dead-man's switch).

Client → server (JSON, add `"id"` to receive `ack`/`error`):

```json
{"type":"input","press":["A"],"left_stick":{"x":0.5,"y":0}}
{"type":"tap","buttons":["a"],"hold_ms":100}
{"type":"macro","steps":[{"buttons":["B"],"duration_ms":50}],"repeat":2}
{"type":"release_all"}   {"type":"ping","id":1}
```

Server → client: `hello` (full status), `state`, `rumble`, `player_lights`, `home_light`, `mode`, `imu_enabled`, `vibration_enabled`, `pong`, `ack`, `error`.

### Rumble event

Sent when the console changes the rumble command of either actuator (and a neutral one on disconnect):

```json
{"type":"rumble","time":1730000000.1,
 "left": {"high":{"frequency":320.0,"amplitude":0.81},"low":{"frequency":160.0,"amplitude":0.81},
          "intensity":0.81,"active":true,"raw":"a8886340"},
 "right":{"...":"same shape"}}
```

`frequency` in Hz, `amplitude` 0–~1 (values above 1.0 are the "unsafe" range games rarely use), `intensity` = strongest band, `raw` = the 4 wire bytes if you want to do your own mapping (e.g. drive a real LRA). Only events after the console enabled vibration are sent.

See [`examples/client.py`](examples/client.py) for a complete client.

## Development without hardware

```bash
.venv/bin/pytest                                   # protocol, core, API, macros (no Bluetooth needed)
.venv/bin/nxremote serve --backend virtual --auto-rumble   # simulated Switch; pressing A makes it rumble
```

The simulated Switch (`nxremote/virtual.py`) performs the real handshake (device info, SPI reads, input mode, IMU, vibration, player lights) over socket pairs, so the same core and API code paths run as on hardware.

## Layout

```
src/nxremote/hid/      pure protocol: reports, subcommands, SPI image, stick calibration, IMU, rumble codec
src/nxremote/core.py   connection lifecycle (listen/reconnect) + real-time report loop (own thread)
src/nxremote/bt/       BlueZ over D-Bus (dbus-fast), SDP record, L2CAP sockets, host setup/doctor
src/nxremote/hub.py    thread-safe input state with overlays; macros.py timed sequences
src/nxremote/api/      FastAPI app, models, web UI
```

## Honest limitations

* **Not verified against a real Switch by the author's test setup.** The protocol follows the public reverse-engineering work and passes the unit/integration suite against a simulated console, but the Bluetooth layer (BlueZ calls, L2CAP, pairing) can only be exercised on a Pi + Switch. Expect to iterate on adapter-specific quirks; please report issues with `--log-level debug` output.
* NFC/IR are not emulated. The IMU is whatever your client sends (no physics).
* Switch 2 compatibility is untested.
* Using this on online games may violate terms of service; use it responsibly (accessibility, automation of single-player tasks, research).

## Security

Anyone with the token can play on your console. Keep the API on a trusted network or behind a VPN/reverse proxy with TLS (the token is sent in clear text over plain HTTP). The service runs as root because Bluetooth HID needs it.

## License

MIT, see [LICENSE](LICENSE) and [NOTICE](NOTICE).
