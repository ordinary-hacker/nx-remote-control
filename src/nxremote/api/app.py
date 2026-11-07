"""FastAPI application: REST + WebSocket remote control of the emulated controller."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from .. import __version__
from ..core import ControllerService
from ..hid import wire
from ..hid.state import NEUTRAL, ControllerState, normalize_buttons
from ..hub import InputHub
from ..macros import MacroRunner, Step, tap_steps
from .models import (
    ConnectRequest,
    InputPatch,
    MacroRequest,
    TapRequest,
)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")


class EventBroker:
    """Fans core-thread events out to asyncio subscribers (WebSocket clients)."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def publish_threadsafe(self, name: str, payload: dict) -> None:
        try:
            self.loop.call_soon_threadsafe(self._publish, {"type": name, **payload})
        except RuntimeError:  # loop closed during shutdown
            pass

    def _publish(self, event: dict) -> None:
        for q in list(self.subscribers):
            if q.full():  # slow consumer: drop the oldest event
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(event)


class Runtime:
    """Everything the endpoints share."""

    def __init__(self, service: ControllerService, hub: InputHub, token: Optional[str], autostart: bool) -> None:
        self.service = service
        self.hub = hub
        self.token = token
        self.autostart = autostart
        self.runner: MacroRunner  # created in lifespan (needs the loop)
        self.broker: EventBroker

    # -- shared operations (used by REST and WebSocket) -------------------------------

    def apply_patch(self, patch: InputPatch) -> ControllerState:
        def fn(s: ControllerState) -> ControllerState:
            buttons = set(s.buttons)
            if patch.buttons is not None:
                buttons = set(normalize_buttons(patch.buttons))
            buttons |= normalize_buttons(patch.press)
            buttons -= normalize_buttons(patch.release)
            changes: dict[str, Any] = {"buttons": frozenset(buttons)}
            if patch.left_stick is not None:
                changes["left_stick"] = patch.left_stick.to_stick()
            if patch.right_stick is not None:
                changes["right_stick"] = patch.right_stick.to_stick()
            if patch.imu is not None:
                changes["imu"] = patch.imu.to_imu()
            if patch.battery is not None:
                changes["battery"] = patch.battery.to_battery()
            return s.with_(**changes)

        return self.hub.modify(fn)

    def replace_input(self, patch: InputPatch) -> ControllerState:
        """Full replacement: everything not mentioned goes back to neutral."""
        self.hub.set_direct(NEUTRAL.with_(battery=self.hub.direct().battery))
        return self.apply_patch(patch)

    def apply_binary(self, data: bytes) -> None:
        """Apply a compact binary input frame (buttons + sticks); keeps imu/battery."""
        decoded = wire.decode(data)
        self.hub.modify(
            lambda s: s.with_(
                buttons=decoded.buttons,
                left_stick=decoded.left_stick,
                right_stick=decoded.right_stick,
            )
        )

    def require_connected(self) -> None:
        if not self.service.status()["connected"]:
            raise HTTPException(409, "controller is not connected to a console")

    def submit_macro(self, steps: list[Step], repeat: int):
        self.require_connected()
        return self.runner.submit(steps, repeat)

    def submit_tap(self, req: TapRequest):
        steps = tap_steps(normalize_buttons(req.buttons), req.hold_ms / 1000, req.release_ms / 1000)
        return self.submit_macro(steps, req.repeat)

    def full_status(self) -> dict:
        cur = self.runner._current
        return {
            "version": __version__,
            "controller": self.service.status(),
            "input": self.hub.snapshot().to_dict(),
            "macros": {
                "running": cur.id if cur else None,
                "pending": sum(1 for j in self.runner.jobs.values() if j.status == "queued"),
            },
        }


def _extract_token(headers, query_token: Optional[str]) -> Optional[str]:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("x-api-key") or query_token


def _token_ok(expected: Optional[str], given: Optional[str]) -> bool:
    if expected is None:
        return True
    return bool(given) and secrets.compare_digest(given.encode(), expected.encode())


def create_app(service: ControllerService, hub: InputHub, token: Optional[str] = None, autostart: bool = True) -> FastAPI:
    rt = Runtime(service, hub, token, autostart)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()
        rt.broker = EventBroker(loop)
        rt.runner = MacroRunner(hub)
        rt.runner.start()
        service.add_listener(rt.broker.publish_threadsafe)
        if rt.autostart:
            await asyncio.to_thread(service.start, True, None)
        try:
            yield
        finally:
            service.remove_listener(rt.broker.publish_threadsafe)
            await rt.runner.close()
            await asyncio.to_thread(service.stop)

    app = FastAPI(
        title="nxremote",
        version=__version__,
        description="Remote-controlled Nintendo Switch Pro Controller emulator.",
        lifespan=lifespan,
    )
    app.state.rt = rt

    @app.exception_handler(ValueError)
    async def _value_error(request: Request, exc: ValueError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    async def auth(request: Request, token: Optional[str] = Query(None, include_in_schema=False)):
        if not _token_ok(rt.token, _extract_token(request.headers, token)):
            raise HTTPException(401, "invalid or missing token", headers={"WWW-Authenticate": "Bearer"})

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(auth)])

    # -- status / connection ---------------------------------------------------------------

    @app.get("/healthz", tags=["meta"])
    async def healthz():
        return {"ok": True, "version": __version__}

    @v1.get("/status", tags=["connection"])
    async def status():
        return rt.full_status()

    @v1.post("/connection/start", status_code=202, tags=["connection"])
    async def connection_start(req: ConnectRequest = ConnectRequest()):
        """Start (or retry) pairing/reconnecting. The first time, open
        *Controllers -> Change Grip/Order* on the Switch."""
        started = await asyncio.to_thread(service.start, req.reconnect, req.address)
        return {"started": started, "state": service.state}

    @v1.post("/connection/stop", tags=["connection"])
    async def connection_stop():
        await asyncio.to_thread(service.stop)
        return {"state": service.state}

    @v1.post("/connection/restart", status_code=202, tags=["connection"])
    async def connection_restart(req: ConnectRequest = ConnectRequest()):
        await asyncio.to_thread(service.stop)
        await asyncio.to_thread(service.start, req.reconnect, req.address)
        return {"state": service.state}

    @v1.delete("/pairings", tags=["connection"])
    async def forget_pairings(address: Optional[str] = None):
        """Forget remembered consoles (and remove them from BlueZ)."""
        removed = set(await asyncio.to_thread(service.store.forget, address))
        removed |= set(await asyncio.to_thread(service.connector.forget, address))
        return {"removed": sorted(removed)}

    # -- input ---------------------------------------------------------------------------------

    @v1.get("/input", tags=["input"])
    async def get_input():
        return {"direct": hub.direct().to_dict(), "effective": hub.snapshot().to_dict()}

    @v1.patch("/input", tags=["input"])
    async def patch_input(patch: InputPatch):
        return {"direct": rt.apply_patch(patch).to_dict()}

    @v1.put("/input", tags=["input"])
    async def put_input(patch: InputPatch):
        return {"direct": rt.replace_input(patch).to_dict()}

    @v1.post("/input/release-all", tags=["input"])
    async def release_all():
        """Neutral buttons, centred sticks, still motion; cancels all macros."""
        rt.runner.cancel_all()
        hub.reset()
        return {"direct": hub.direct().to_dict()}

    @v1.post("/tap", status_code=202, tags=["macros"])
    async def tap(req: TapRequest):
        """Press and release buttons (optionally repeated)."""
        job = rt.submit_tap(req)
        if req.wait:
            await job.done.wait()
        return job.to_dict()

    @v1.post("/macros", status_code=202, tags=["macros"])
    async def macro_create(req: MacroRequest):
        job = rt.submit_macro([s.to_step() for s in req.steps], req.repeat)
        if req.wait:
            await job.done.wait()
        return job.to_dict()

    @v1.get("/macros", tags=["macros"])
    async def macro_list():
        return {"macros": [j.to_dict() for j in rt.runner.jobs.values()]}

    @v1.get("/macros/{job_id}", tags=["macros"])
    async def macro_get(job_id: str):
        job = rt.runner.get(job_id)
        if not job:
            raise HTTPException(404, "no such macro")
        return job.to_dict()

    @v1.delete("/macros/{job_id}", tags=["macros"])
    async def macro_cancel(job_id: str):
        if not rt.runner.get(job_id):
            raise HTTPException(404, "no such macro")
        return {"cancelled": rt.runner.cancel(job_id)}

    @v1.delete("/macros", tags=["macros"])
    async def macro_cancel_all():
        return {"cancelled": rt.runner.cancel_all()}

    # -- rumble ------------------------------------------------------------------------------------

    @v1.get("/rumble", tags=["rumble"])
    async def rumble():
        """Latest rumble command the console sent (HD rumble, per actuator)."""
        return {"last": service.last_rumble}

    app.include_router(v1)

    # -- websocket ------------------------------------------------------------------------------------

    @app.websocket("/v1/ws")
    async def ws_endpoint(
        ws: WebSocket,
        token: Optional[str] = None,
        release_on_close: bool = True,
        events: Optional[str] = None,
    ):
        """Bidirectional channel. Client -> server messages (JSON):

        ``{"type":"input", ...InputPatch}``, ``{"type":"tap", ...}``, ``{"type":"macro", ...}``,
        ``{"type":"release_all"}``, ``{"type":"ping"}``. Add ``"id"`` to get an ``ack``/``error`` back.
        Server -> client: ``hello``, ``state``, ``rumble``, ``player_lights``, ``home_light``,
        ``mode``, ``imu_enabled``, ``vibration_enabled``, ``pong``, ``ack``, ``error``.
        Query: ``events=rumble,state`` filters events; ``release_on_close=false`` keeps inputs held after
        this socket closes.
        """
        if not _token_ok(rt.token, _extract_token(ws.headers, token)):
            await ws.close(code=1008)
            return
        await ws.accept()
        # Note: asyncio already sets TCP_NODELAY on every TCP transport, and the
        # server disables permessage-deflate for this realtime channel (see cli.py),
        # so tiny input frames are sent immediately and uncompressed.
        wanted = {e.strip() for e in events.split(",")} if events else None
        q = rt.broker.subscribe()
        sent_input = False
        await ws.send_json({"type": "hello", **rt.full_status()})

        async def pump() -> None:
            while True:
                ev = await q.get()
                if wanted is None or ev["type"] in wanted:
                    await ws.send_json(ev)

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                # Fast path: a compact binary input frame (buttons + sticks).
                if message.get("bytes") is not None:
                    data = message["bytes"]
                    try:
                        if wire.is_binary_input(data):
                            rt.apply_binary(data)
                            sent_input = True
                        else:
                            await ws.send_json({"type": "error", "message": "unknown binary frame"})
                    except (ValueError, HTTPException) as e:
                        detail = e.detail if isinstance(e, HTTPException) else str(e)
                        await ws.send_json({"type": "error", "message": detail})
                    continue
                raw = message.get("text")
                if raw is None:
                    continue
                msg_id = None
                try:
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        raise ValueError("message must be a JSON object")
                    msg_id = msg.pop("id", None)
                    kind = msg.pop("type", None)
                    result: dict = {}
                    if kind == "input":
                        rt.apply_patch(InputPatch.model_validate(msg))
                        sent_input = True
                    elif kind == "tap":
                        result = rt.submit_tap(TapRequest.model_validate(msg)).to_dict()
                    elif kind == "macro":
                        m = MacroRequest.model_validate(msg)
                        result = rt.submit_macro([s.to_step() for s in m.steps], m.repeat).to_dict()
                    elif kind == "release_all":
                        rt.runner.cancel_all()
                        hub.reset()
                    elif kind == "ping":
                        await ws.send_json({"type": "pong", "id": msg_id})
                        continue
                    else:
                        raise ValueError(f"unknown message type {kind!r}")
                    if msg_id is not None:
                        await ws.send_json({"type": "ack", "id": msg_id, **result})
                except (ValueError, ValidationError, HTTPException) as e:
                    detail = e.detail if isinstance(e, HTTPException) else str(e)
                    await ws.send_json({"type": "error", "id": msg_id, "message": detail})
        except WebSocketDisconnect:
            pass
        finally:
            pump_task.cancel()
            rt.broker.unsubscribe(q)
            if sent_input and release_on_close:
                hub.reset()

    # -- tiny built-in web UI ---------------------------------------------------------------------------

    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    async def index():
        page = STATIC_DIR / "index.html"
        if not page.exists():
            return HTMLResponse("<h1>nxremote</h1><p>See <a href='/docs'>/docs</a>.</p>")
        return HTMLResponse(page.read_text())

    return app
