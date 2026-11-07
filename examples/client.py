"""Minimal nxremote client: press buttons, stream a stick, react to rumble.

    pip install websockets
    python examples/client.py ws://raspberrypi.local:8000 YOUR_TOKEN
"""

import asyncio
import json
import sys

import websockets


async def main(base: str, token: str) -> None:
    async with websockets.connect(f"{base}/v1/ws?token={token}") as ws:
        hello = json.loads(await ws.recv())
        print("controller:", hello["controller"]["state"])

        async def listen() -> None:
            async for raw in ws:
                ev = json.loads(raw)
                if ev["type"] == "rumble":
                    print(f"rumble L={ev['left']['intensity']:.2f} R={ev['right']['intensity']:.2f}",
                          f"(L high {ev['left']['high']['frequency']:.0f} Hz / low {ev['left']['low']['frequency']:.0f} Hz)")
                elif ev["type"] in ("state", "player_lights", "vibration_enabled"):
                    print(ev["type"], {k: v for k, v in ev.items() if k != "type"})

        listener = asyncio.create_task(listen())

        # A tap handled by the Pi's macro runner (precise timing, even over WiFi).
        await ws.send(json.dumps({"type": "tap", "buttons": ["A"], "hold_ms": 100}))

        # Hold ZR while sweeping the left stick right, then release everything.
        await ws.send(json.dumps({"type": "input", "press": ["ZR"]}))
        for i in range(21):
            await ws.send(json.dumps({"type": "input", "left_stick": {"x": i / 20, "y": 0}}))
            await asyncio.sleep(0.02)
        await ws.send(json.dumps({"type": "release_all"}))

        # A "shake": gyro pulse as a one-shot macro.
        shake = {"type": "macro", "steps": [
            {"imu": {"accel": [0, 0, 1], "gyro": [0, 0, 500]}, "duration_ms": 60},
            {"imu": {"accel": [0, 0, 1], "gyro": [0, 0, -500]}, "duration_ms": 60},
        ], "repeat": 5}
        await ws.send(json.dumps(shake))

        await asyncio.sleep(5)  # keep listening for rumble
        listener.cancel()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8000",
                     sys.argv[2] if len(sys.argv) > 2 else ""))
