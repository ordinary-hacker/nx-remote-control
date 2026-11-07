"""Timed input sequences ("macros"): taps, holds, stick sweeps, motion gestures.

A macro is a list of :class:`Step`. Each step holds a set of inputs for a
duration; steps run back to back with no gap, so releases are explicit (a step
with no buttons is a release/wait). Jobs run strictly FIFO in one worker so
they never fight each other; they are layered over the direct input state
through the hub's overlay mechanism.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .hid.state import Imu, Stick
from .hub import InputHub, Overlay

MAX_STEPS = 10_000
MAX_JOBS_KEPT = 200


@dataclass(frozen=True)
class Step:
    duration: float  # seconds
    overlay: Overlay = field(default_factory=Overlay)


@dataclass
class Job:
    id: str
    steps: list[Step]
    repeat: int
    status: str = "queued"  # queued | running | done | cancelled | failed
    created: float = field(default_factory=time.time)
    started: Optional[float] = None
    finished: Optional[float] = None
    iteration: int = 0
    error: Optional[str] = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def total_duration(self) -> float:
        return sum(s.duration for s in self.steps) * self.repeat

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "steps": len(self.steps),
            "repeat": self.repeat,
            "iteration": self.iteration,
            "duration_s": round(self.total_duration, 3),
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "error": self.error,
        }


def tap_steps(buttons: frozenset[str], hold: float, release: float) -> list[Step]:
    steps = [Step(hold, Overlay(buttons=buttons))]
    if release > 0:
        steps.append(Step(release))
    return steps


class MacroRunner:
    def __init__(self, hub: InputHub) -> None:
        self.hub = hub
        self.jobs: dict[str, Job] = {}
        self._queue: "asyncio.Queue[Job]" = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None
        self._current: Optional[Job] = None

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="macro-runner")

    async def close(self) -> None:
        self.cancel_all()
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    # -- API -------------------------------------------------------------------

    def submit(self, steps: list[Step], repeat: int = 1) -> Job:
        if not steps:
            raise ValueError("a macro needs at least one step")
        if len(steps) > MAX_STEPS:
            raise ValueError(f"at most {MAX_STEPS} steps")
        job = Job(uuid.uuid4().hex[:8], steps, max(1, repeat))
        self.jobs[job.id] = job
        self._queue.put_nowait(job)
        self._prune()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        job.cancel.set()
        if job.status == "queued":
            self._finish(job, "cancelled")
        return True

    def cancel_all(self) -> int:
        n = 0
        for job in list(self.jobs.values()):
            if self.cancel(job.id):
                n += 1
        return n

    def _prune(self) -> None:
        finished = [j for j in self.jobs.values() if j.status in ("done", "cancelled", "failed")]
        for j in finished[: max(0, len(self.jobs) - MAX_JOBS_KEPT)]:
            self.jobs.pop(j.id, None)

    # -- worker ------------------------------------------------------------------

    def _finish(self, job: Job, status: str, error: Optional[str] = None) -> None:
        job.status = status
        job.error = error
        job.finished = time.time()
        job.done.set()

    async def _sleep_until(self, job: Job, deadline: float) -> bool:
        """Sleep until the monotonic deadline. Returns False if cancelled."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return not job.cancel.is_set()
        try:
            await asyncio.wait_for(job.cancel.wait(), remaining)
            return False
        except asyncio.TimeoutError:
            return True

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            if job.status != "queued":
                continue
            self._current = job
            job.status = "running"
            job.started = time.time()
            oid = self.hub.push_overlay(Overlay())
            try:
                deadline = time.monotonic()
                cancelled = False
                for it in range(job.repeat):
                    job.iteration = it + 1
                    for step in job.steps:
                        self.hub.replace_overlay(oid, step.overlay)
                        deadline += step.duration
                        if not await self._sleep_until(job, deadline):
                            cancelled = True
                            break
                    if cancelled:
                        break
                self._finish(job, "cancelled" if cancelled else "done")
            except asyncio.CancelledError:
                self._finish(job, "cancelled")
                raise
            except Exception as e:  # pragma: no cover
                self._finish(job, "failed", f"{type(e).__name__}: {e}")
            finally:
                self.hub.pop_overlay(oid)
                self._current = None


def make_overlay(
    buttons: frozenset[str],
    left: Optional[Stick] = None,
    right: Optional[Stick] = None,
    imu: Optional[Imu] = None,
) -> Overlay:
    return Overlay(buttons, left.clamped() if left else None, right.clamped() if right else None, imu)
