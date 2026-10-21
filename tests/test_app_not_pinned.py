"""A built app must be collectable once nothing references it. Exit hooks
registered with a plain ``atexit.register(obj.stop)`` kept every app a test
built alive until the worker exited, so a CI coverage worker grew past 7 GB
and the 16 GB runner was lost near the end of the run.

A few module-level slots (app.artwork, app.commons) point at the most
recently built app, so the check builds a second one and asserts the
first is gone."""

from __future__ import annotations

import gc
import time
import weakref
from pathlib import Path

from app import background_loops
from app.main import REPO_ROOT, create_app


def _build(root: Path) -> weakref.ref:
    app = create_app(
        testing=False,
        data_root=root,
        plugins_dir=REPO_ROOT / "plugins",
        renderers_dir=REPO_ROOT / "renderers",
    )
    return weakref.ref(app)


def test_an_earlier_app_is_freed(tmp_path: Path) -> None:
    first = _build(tmp_path / "a")
    _build(tmp_path / "b")
    background_loops.stop_all()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        gc.collect()
        if first() is None:
            break
        time.sleep(0.05)
    assert first() is None
