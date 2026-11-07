# nxremote — Android app

A native Android app that forwards a **physical game controller connected to your
phone/tablet** over the internet to an [`nxremote`](../README.md) host, which relays
it to a Nintendo Switch. The Switch's rumble is played back on the device, and —
unlike the browser page — it **keeps forwarding in the background** (switch apps,
lock the screen) via a foreground service.

```
  gamepad ──USB/Bluetooth──► Android app ──Internet (WebSocket)──► nxremote host ──BT──► Switch
     ▲        (foreground service keeps this alive in the background)                     │
     └──────────────────────────────── rumble ◄────────────────────────────────────────┘
```

## Why a native app (and not the browser or Mobiler)

A mobile **browser** can't keep forwarding once you leave the tab (background tabs
are suspended and the Gamepad API stops when hidden). Truly staying alive in the
background is a native-app capability: this app runs a **foreground service** with
an ongoing notification — the Android-sanctioned way to keep a user-started
connection running — so it behaves like a messaging app that stays connected.

(It is plain Kotlin, not Mobiler/Rust: Mobiler is built for API-backed UI apps and
has no way to read a `InputDevice` game controller or run a persistent foreground
service, so it was the wrong tool for this job.)

## What it does

- Reads any Android-recognised controller (Xbox, DualShock/DualSense, Switch Pro,
  8BitDo, generic USB/Bluetooth pads) via `InputDevice`/`KeyEvent`/`MotionEvent`:
  buttons, both sticks, analog triggers (→ ZL/ZR), and the D-pad HAT.
- Maps them to the host's Switch button names (physical-position convention by
  default: the bottom face button is Switch **B**, matching a real Switch).
- Streams input diffs over a WebSocket to the host (OkHttp, auto-reconnect with
  backoff, 20 s pings, 120 Hz send loop with a 1 s keep-alive).
- Plays the Switch's rumble back on the device's vibrator.
- Keeps running when backgrounded/locked via a foreground service + partial wake
  lock, with a persistent notification that has a **Stop** action.

## Build

Requirements: Android SDK (platform 34/35, build-tools 35+), JDK 17+ (JDK 21 used
here). From `android/`:

```bash
# point the build at your SDK (or set ANDROID_HOME / sdk.dir in local.properties)
echo "sdk.dir=$ANDROID_HOME" > local.properties

./gradlew :app:assembleDebug
# -> app/build/outputs/apk/debug/app-debug.apk
```

Install on a device with USB debugging:

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

## Use

1. Pair or plug a controller into the phone.
2. Open the app, enter your host as `IP[:port]` (e.g. `192.168.1.50:8000`) or a
   full `ws(s)://…` URL, and the API token (leave blank if the host runs
   `--no-auth`). Get the token on the host with `nxremote token`.
3. Tap **Start forwarding**. Status shows `switch:connected` once the host's own
   Bluetooth link to the Switch is up. Keep the app's screen focused so Android
   delivers the controller events to it; the service holds the connection even
   when you switch away.

Cleartext (`ws://`, plain HTTP) is allowed so it works on a home LAN; for use over
the internet put the host behind TLS and give a `wss://` URL.

## How it's built

| File | Role |
|---|---|
| `ControllerState.kt` | input/rumble data model; `toInputJson()` matches the host's `{"type":"input"}` |
| `ControllerMapping.kt` | Android `KeyEvent`/`MotionEvent` → Switch button names, deadzone, trigger threshold |
| `ControllerHolder.kt` | thread-safe bridge: Activity publishes input, service reads it; holds latest rumble |
| `NxWebSocketClient.kt` | OkHttp WebSocket: `HostUrl.build`, send state, parse rumble/state, reconnect |
| `ForwarderService.kt` | foreground service (`dataSync`), 120 Hz send loop, rumble playback, wake lock, notification |
| `MainActivity.kt` | host/token UI, captures controller events, starts/stops the service, shows status |

## Latency

Tuned for input lag: Nagle is disabled on the socket (a `SocketFactory` that sets
`TCP_NODELAY`), the send loop blocks until the input actually changes and fires
within ~1 ms (rather than waiting out a fixed tick), and input is sent as the
host's **12-byte binary frame** (vs ~98 bytes of JSON, and no parse on the host).
Keep the phone and host on the same LAN / a low-latency link for the best feel.

## Verified / not verified

**Verified** on an Android 14 (API 34) x86_64 emulator: the app installs, launches,
requests the notifications permission, and — driven through its real UI — the
foreground service connects to a host (`WebSocket /v1/ws [accepted]`, UI shows
`switch:connected`) and **keeps the service alive and connected after the app is
backgrounded** (Home + opening another app; service stays `isForeground`, process
alive, socket not dropped).

**Device-only (not tested here):** reading a *physical* controller and playing
rumble can't be exercised on an emulator with no attached pad, so those paths are
validated by compilation and the Android input/vibrator APIs, not by a live pad.
Face-button position vs. label mapping and trigger thresholds may need tuning per
controller; the mapping is in `ControllerMapping.kt`.

## Limitations

- **iOS**: not provided. iOS has no App Store-compliant way to keep a socket +
  controller bridge running in the background, so the equivalent can only be a
  foreground-only app; use the host's browser page on iOS instead.
- Battery: continuous forwarding + wake lock uses power; stop when you're done
  (notification → Stop).

## License

MIT (same as the host). See [../NOTICE](../NOTICE).
