"""Background jobs, shown in a tray at the bottom right of every admin page.

Anything that works in the background (a widget painting pictures, a sync, an
import) reports here, and the admin shell draws one card per job with a
progress bar. The registry lives in the server process, so the tray survives
navigating between pages: each page load asks for the current jobs again.

For a widget's ``server.py``::

    try:
        from app import jobs
    except ImportError:   # an older Tesserae, or a host without the tray
        jobs = None

    if jobs:
        jobs.report("my_widget", "sync", "Syncing photos", progress=0.4,
                    detail="40 of 100", stop=cancel)      # 0..1, or None
    ...
    if jobs:
        jobs.finish("my_widget", "sync", "100 photos")    # or jobs.fail(...)

A job is named by ``(source, key)``; calling ``report`` again with the same
pair updates it, so no handle needs carrying about. ``progress=None`` draws an
indeterminate bar. ``action`` is an optional ``(label, callable)``: a button of
the job's own on the card (a widget's "Paint now"), pressed through ``/jobs/act``.
``stop`` is an optional callable that gives up the job; with
one, the card has a Stop button. A finished job stays on screen for a few
seconds, a failed one longer, and a job nobody has touched for a quarter of an
hour is taken to have died with its worker and is dropped.

mypy --strict applies to this module, see pyproject.toml.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from flask import Blueprint, Flask, Response, abort, jsonify, request

logger = logging.getLogger(__name__)

RUNNING = "running"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"

# How long a finished card stays, by outcome, and how long a running job may
# go unreported before it is taken to be dead.
LINGER_S: dict[str, float] = {DONE: 6.0, STOPPED: 4.0, FAILED: 25.0}
STALE_S = 900.0
MAX_JOBS = 12


@dataclass
class Job:
    source: str
    key: str
    title: str
    label: str = ""
    detail: str = ""
    progress: float | None = None
    state: str = RUNNING
    href: str = ""
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    stop: Callable[[], object] | None = None
    # A button of the job's own: its label, and what pressing it runs (a widget's "Paint now").
    action: tuple[str, Callable[[], object]] | None = None

    @property
    def id(self) -> str:
        return f"{self.source}:{self.key}"


class JobBoard:
    """The jobs now running or just finished, safe to use from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def report(
        self,
        source: str,
        key: str,
        title: str,
        *,
        progress: float | None = None,
        detail: str = "",
        label: str = "",
        href: str = "",
        stop: Callable[[], object] | None = None,
        action: tuple[str, Callable[[], object]] | None = None,
    ) -> None:
        """Start a job, or update it when ``(source, key)`` is already there."""
        now = time.time()
        pct = None if progress is None else min(1.0, max(0.0, float(progress)))
        with self._lock:
            job = self._jobs.get(f"{source}:{key}")
            if job is None or job.state != RUNNING:
                job = Job(source=source, key=key, title=title, started_at=now)
                self._jobs[job.id] = job
            job.title = title[:120]
            job.detail = detail[:200]
            job.progress = pct
            job.updated_at = now
            if label:
                job.label = label[:60]
            if href:
                job.href = href
            job.stop = stop
            job.action = action
            self._trim()

    def _end(self, source: str, key: str, state: str, detail: str) -> None:
        with self._lock:
            job = self._jobs.get(f"{source}:{key}")
            if job is None:
                return
            job.state = state
            job.stop = None
            job.action = None
            job.updated_at = time.time()
            if state == DONE:
                job.progress = 1.0
            if detail:
                job.detail = detail[:200]

    def finish(self, source: str, key: str, detail: str = "") -> None:
        self._end(source, key, DONE, detail)

    def fail(self, source: str, key: str, detail: str = "") -> None:
        self._end(source, key, FAILED, detail)

    def cancel(self, source: str, key: str, detail: str = "") -> None:
        """The job was given up (by the person, usually)."""
        self._end(source, key, STOPPED, detail)

    def clear(self, source: str, key: str) -> None:
        with self._lock:
            self._jobs.pop(f"{source}:{key}", None)

    def stop(self, job_id: str) -> bool:
        """Run a job's stop callable. False when there is none to run."""
        with self._lock:
            job = self._jobs.get(job_id)
            callback = job.stop if job and job.state == RUNNING else None
        if callback is None:
            return False
        try:
            callback()
        except Exception:
            logger.exception("stopping job %s failed", job_id)
            return False
        if job is not None:
            self.cancel(job.source, job.key)
        return True

    def act(self, job_id: str) -> bool:
        """Press a job's own button. False when it has none to press."""
        with self._lock:
            job = self._jobs.get(job_id)
            callback = job.action[1] if job and job.action and job.state == RUNNING else None
        if callback is None:
            return False
        try:
            callback()
        except Exception:
            logger.exception("job %s action failed", job_id)
            return False
        return True

    def _trim(self) -> None:
        if len(self._jobs) <= MAX_JOBS:
            return
        oldest = sorted(self._jobs.values(), key=lambda j: (j.state == RUNNING, j.updated_at))
        for job in oldest[: len(self._jobs) - MAX_JOBS]:
            self._jobs.pop(job.id, None)

    def snapshot(self) -> list[dict[str, object]]:
        now = time.time()
        out: list[dict[str, object]] = []
        with self._lock:
            for job in list(self._jobs.values()):
                age = now - job.updated_at
                if job.state == RUNNING and age > STALE_S:
                    del self._jobs[job.id]
                    continue
                if job.state != RUNNING and age > LINGER_S.get(job.state, 6.0):
                    del self._jobs[job.id]
                    continue
                out.append(
                    {
                        "id": job.id,
                        "source": job.source,
                        "label": job.label,
                        "title": job.title,
                        "detail": job.detail,
                        "progress": job.progress,
                        "state": job.state,
                        "href": job.href,
                        "stoppable": job.stop is not None and job.state == RUNNING,
                        "action": job.action[0][:40] if job.action and job.state == RUNNING else "",
                        "started": job.started_at,
                        "seconds": round(now - job.started_at),
                    }
                )
        out.sort(key=lambda j: float(str(j["started"])))
        return out


_BOARD = JobBoard()


def board() -> JobBoard:
    return _BOARD


def report(
    source: str,
    key: str,
    title: str,
    *,
    progress: float | None = None,
    detail: str = "",
    label: str = "",
    href: str = "",
    stop: Callable[[], object] | None = None,
    action: tuple[str, Callable[[], object]] | None = None,
) -> None:
    _BOARD.report(
        source,
        key,
        title,
        progress=progress,
        detail=detail,
        label=label,
        href=href,
        stop=stop,
        action=action,
    )


def finish(source: str, key: str, detail: str = "") -> None:
    _BOARD.finish(source, key, detail)


def fail(source: str, key: str, detail: str = "") -> None:
    _BOARD.fail(source, key, detail)


def cancel(source: str, key: str, detail: str = "") -> None:
    _BOARD.cancel(source, key, detail)


def clear(source: str, key: str) -> None:
    _BOARD.clear(source, key)


bp = Blueprint("jobs", __name__, url_prefix="/jobs")


@bp.get("/jobs.json")
def listing() -> Response:
    """The jobs the tray draws. Polled by the admin shell while a tab is visible."""
    response = jsonify({"jobs": _BOARD.snapshot()})
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.post("/stop")
def stop() -> Response:
    body = request.get_json(silent=True) or {}
    job_id = str(body.get("id") or "")
    if not job_id:
        abort(400)
    return jsonify({"stopped": _BOARD.stop(job_id)})


@bp.post("/act")
def act() -> Response:
    body = request.get_json(silent=True) or {}
    job_id = str(body.get("id") or "")
    if not job_id:
        abort(400)
    return jsonify({"acted": _BOARD.act(job_id)})


def register(app: Flask) -> None:
    app.register_blueprint(bp)
