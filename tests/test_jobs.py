"""Background-job board: the registry, the JSON the tray polls, and the shell wiring."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from app import jobs
from app.jobs import JobBoard
from app.main import REPO_ROOT, create_app


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    a = create_app(
        testing=False,
        data_root=tmp_path,
        plugins_dir=REPO_ROOT / "plugins",
        renderers_dir=REPO_ROOT / "renderers",
        devices_dir=REPO_ROOT / "devices",
    )
    a.config["TESTING"] = True
    return a


def _sign_in(client: Any) -> None:
    client.post("/setup", data={"password": "abcdefgh", "password_confirm": "abcdefgh"})


def test_report_updates_in_place() -> None:
    b = JobBoard()
    b.report("w", "k", "Painting", progress=0.25, detail="1 of 4")
    first = b.snapshot()[0]["started"]
    b.report("w", "k", "Painting", progress=0.5, detail="2 of 4")
    (job,) = b.snapshot()
    assert job["progress"] == 0.5 and job["detail"] == "2 of 4"
    assert job["started"] == first
    assert job["id"] == "w:k" and job["state"] == "running"


def test_progress_is_clamped_and_none_is_indeterminate() -> None:
    b = JobBoard()
    b.report("w", "a", "A", progress=7)
    b.report("w", "b", "B", progress=-1)
    b.report("w", "c", "C")
    by_key = {j["id"]: j["progress"] for j in b.snapshot()}
    assert by_key == {"w:a": 1.0, "w:b": 0.0, "w:c": None}


def test_finish_fills_the_bar_and_lingers_then_goes(monkeypatch: pytest.MonkeyPatch) -> None:
    b = JobBoard()
    b.report("w", "k", "Painting", progress=0.4, stop=lambda: None)
    b.finish("w", "k", "8 painted")
    (job,) = b.snapshot()
    assert job["state"] == "done" and job["progress"] == 1.0
    assert job["detail"] == "8 painted" and job["stoppable"] is False
    real = jobs.time.time
    monkeypatch.setattr(jobs.time, "time", lambda: real() + jobs.LINGER_S[jobs.DONE] + 1)
    assert b.snapshot() == []


def test_failed_lingers_longer_than_done(monkeypatch: pytest.MonkeyPatch) -> None:
    b = JobBoard()
    b.report("w", "k", "Sync")
    b.fail("w", "k", "no network")
    real = jobs.time.time
    monkeypatch.setattr(jobs.time, "time", lambda: real() + jobs.LINGER_S[jobs.DONE] + 1)
    assert [j["state"] for j in b.snapshot()] == ["failed"]


def test_unreported_running_job_is_dropped_as_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    b = JobBoard()
    b.report("w", "k", "Sync")
    real = jobs.time.time
    monkeypatch.setattr(jobs.time, "time", lambda: real() + jobs.STALE_S + 1)
    assert b.snapshot() == []


def test_reporting_a_finished_job_again_starts_a_fresh_one() -> None:
    b = JobBoard()
    b.report("w", "k", "Sync")
    b.finish("w", "k")
    old = b.snapshot()[0]["started"]
    b.report("w", "k", "Sync", progress=0.1)
    (job,) = b.snapshot()
    assert job["state"] == "running" and float(str(job["started"])) >= float(str(old))


def test_stop_runs_the_callback_and_marks_stopped() -> None:
    b = JobBoard()
    calls: list[int] = []
    b.report("w", "k", "Sync", stop=lambda: calls.append(1))
    assert b.snapshot()[0]["stoppable"] is True
    assert b.stop("w:k") is True
    assert calls == [1]
    assert b.snapshot()[0]["state"] == "stopped"
    assert b.stop("w:k") is False


def test_stop_without_a_callback_or_with_a_failing_one() -> None:
    b = JobBoard()
    b.report("w", "plain", "Plain")
    assert b.stop("w:plain") is False

    def boom() -> None:
        raise RuntimeError("nope")

    b.report("w", "boom", "Boom", stop=boom)
    assert b.stop("w:boom") is False
    assert [j["state"] for j in b.snapshot() if j["id"] == "w:boom"] == ["running"]


def test_board_is_capped() -> None:
    b = JobBoard()
    for i in range(jobs.MAX_JOBS + 5):
        b.report("w", str(i), f"J{i}")
    assert len(b.snapshot()) == jobs.MAX_JOBS


def test_module_helpers_use_the_shared_board() -> None:
    jobs.report("test_jobs", "k", "Shared", progress=0.5)
    try:
        assert any(j["id"] == "test_jobs:k" for j in jobs.board().snapshot())
        jobs.finish("test_jobs", "k")
        assert [j["state"] for j in jobs.board().snapshot() if j["id"] == "test_jobs:k"] == ["done"]
    finally:
        jobs.clear("test_jobs", "k")


def test_endpoints_list_and_stop(app: Flask) -> None:
    client = app.test_client()
    _sign_in(client)
    hit: list[int] = []
    jobs.report("test_jobs", "ep", "Endpoint job", progress=0.3, stop=lambda: hit.append(1))
    try:
        r = client.get("/jobs/jobs.json")
        assert r.status_code == 200
        assert r.headers["Cache-Control"] == "no-store"
        ids = [j["id"] for j in r.get_json()["jobs"]]
        assert "test_jobs:ep" in ids
        assert client.post("/jobs/stop", json={}).status_code == 400
        r = client.post("/jobs/stop", json={"id": "test_jobs:ep"})
        assert r.get_json() == {"stopped": True}
        assert hit == [1]
    finally:
        jobs.clear("test_jobs", "ep")


def test_endpoints_need_sign_in(app: Flask) -> None:
    client = app.test_client()
    _sign_in(client)
    client.post("/logout")
    r = client.get("/jobs/jobs.json")
    assert r.status_code in (301, 302, 303, 401, 403)


def test_shell_carries_the_tray(app: Flask) -> None:
    client = app.test_client()
    _sign_in(client)
    html = client.get("/", follow_redirects=True).get_data(as_text=True)
    assert 'id="job-tray"' in html
    assert "jobs-tray.js" in html and "style/jobs.css" in html
