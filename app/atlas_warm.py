"""Building glyph atlases ahead of the device that needs them (#359).

An atlas is static per (size, weight, charset), so it only has to be built
once. Device requests stay cache-only (a /frame/spec or manifest poll must not
wait on a browser render), and this module fills the cache out of band from
three places: at startup for the fixed touch-v3 roles, after a render for the
overlay sizes that composition uses, and from a device poll that found one
missing. All three share one de-dup guard, so the same atlas is never built
twice at once.

A job is ``("touch", role_id)`` or ``("overlay", px, weight)``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AtlasJob = tuple[Any, ...]

_warming: set[AtlasJob] = set()
_lock = threading.Lock()


def schedule(app_obj: Any, jobs: Iterable[AtlasJob], base_url: str) -> None:
    """Build the given atlases in a daemon thread, skipping any already being
    built. Never blocks the caller."""
    with _lock:
        todo = [j for j in dict.fromkeys(jobs) if j not in _warming]
        _warming.update(todo)
    if not todo:
        return
    threading.Thread(target=_run, args=(app_obj, todo, base_url), daemon=True).start()


def _run(app_obj: Any, jobs: list[AtlasJob], base_url: str) -> None:
    try:
        renders_dir = app_obj.config.get("RENDERS_DIR")
        if renders_dir is None:
            return
        from app.overlay_sync import browser_rasterizer, build_atlas
        from app.touch_atlas import build_touch_atlas

        rasterize = app_obj.config.get("OVERLAY_ATLAS_RASTERIZER") or browser_rasterizer(base_url)
        with app_obj.app_context():
            for job in jobs:
                try:
                    if job[0] == "touch":
                        build_touch_atlas(
                            job[1], renders_dir=Path(renders_dir), rasterize=rasterize
                        )
                    elif job[0] == "overlay":
                        build_atlas(
                            int(job[1]),
                            int(job[2]),
                            renders_dir=Path(renders_dir),
                            rasterize=rasterize,
                        )
                except Exception:
                    logger.exception("atlas warm failed %s", job)
    finally:
        with _lock:
            _warming.difference_update(jobs)


def touch_jobs(roles: Iterable[str]) -> list[AtlasJob]:
    return [("touch", r) for r in roles]


def overlay_jobs(slots: Iterable[Any]) -> list[AtlasJob]:
    """The (px, weight) atlases a composition's overlay slots will ask for."""
    jobs: list[AtlasJob] = []
    for s in slots:
        if not isinstance(s, dict):
            continue
        try:
            jobs.append(("overlay", int(s["px"]), int(s["weight"])))
        except (KeyError, TypeError, ValueError):
            continue
    return jobs


# The strip page is captured from this server over loopback, so a warm queued
# while the app is still being built waits for it to start listening.
STARTUP_DELAY_S = 20.0


def warm_at_startup(app_obj: Any, base_url_fn: Callable[[], str]) -> None:
    """Queue the fixed touch-v3 roles shortly after the server starts, so the
    first poll after a boot or an update already finds them (an update moves
    the build tag, see overlay_sync.ATLAS_FORMAT, and rebuilds each once).
    Never fatal; a failure leaves the poll-time fallback."""
    from app.touch_atlas import TOUCH_ATLAS_ROLES

    def later() -> None:
        import time

        time.sleep(STARTUP_DELAY_S)
        try:
            schedule(app_obj, touch_jobs(TOUCH_ATLAS_ROLES), base_url_fn())
        except Exception:
            logger.exception("startup atlas warm failed")

    threading.Thread(target=later, daemon=True).start()
