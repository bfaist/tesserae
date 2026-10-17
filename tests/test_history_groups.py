"""History day timeline grouping.

Rows bucket by the local calendar day they happened on (in the app
timezone) with "Today" / "Yesterday" / dated headings and per-day push and
failure counts; "By dashboard" swaps the buckets for one group per target.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from flask import Flask

from app.history_routes import day_label, group_history
from app.main import REPO_ROOT, create_app
from app.state.event_log import EventLog

MEL = ZoneInfo("Australia/Melbourne")


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
    a.config["SETTINGS_STORE"].update_section("app", {"timezone": "Australia/Melbourne"})
    return a


def _sign_in(client) -> None:
    client.post("/setup", data={"password": "abcdefgh", "password_confirm": "abcdefgh"})


def _record_at(log: EventLog, ts: float, **fields) -> int:
    """Record a push row, then pin its timestamp (``record`` stamps now)."""
    defaults = {"source": "page", "status": "sent", "digest": "d"}
    defaults.update(fields)
    rid = log.record(type="push", **defaults)
    with log._lock, log._conn() as conn:
        conn.execute("UPDATE events SET timestamp = ? WHERE id = ?", (ts, rid))
        conn.commit()
    return rid


def _row(ts: float, *, target: str = "home", failed: bool = False) -> dict:
    return {"timestamp": ts, "target": target, "failed": failed}


def test_day_label_today_yesterday_then_dates() -> None:
    today = date(2026, 9, 18)
    assert day_label(date(2026, 9, 18), today) == "Today"
    assert day_label(date(2026, 9, 17), today) == "Yesterday"
    assert day_label(date(2026, 9, 15), today) == "Tue 15 Sep"
    # A day in another year carries it.
    assert day_label(date(2025, 12, 31), today) == "Wed 31 Dec 2025"


def test_group_by_day_uses_the_app_timezone_and_counts_failures() -> None:
    # 2026-09-18 10:00 Melbourne (AEST, UTC+10) is 00:00 UTC the same day.
    now = datetime(2026, 9, 18, 10, 0, tzinfo=MEL).timestamp()
    rows = [
        _row(now - 60),  # today
        _row(now - 3 * 3600, failed=True),  # today, 07:00 local
        # 23:30 local the day before, which is 13:30 UTC: still "Yesterday"
        # in Melbourne, though the UTC date matches the first two rows.
        _row(datetime(2026, 9, 17, 23, 30, tzinfo=MEL).timestamp()),
        _row(datetime(2026, 9, 15, 9, 0, tzinfo=MEL).timestamp(), failed=True),
    ]
    groups = group_history(rows, by="day", tz=MEL, now=now)
    assert [g["label"] for g in groups] == ["Today", "Yesterday", "Tue 15 Sep"]
    assert [(g["count"], g["failed"]) for g in groups] == [(2, 1), (1, 0), (1, 1)]
    assert groups[0]["key"] == "2026-09-18"

    # The same rows read in UTC bucket differently: "now" is 00:00 UTC on
    # the 18th, so the three recent rows all fall on the 17th (Yesterday)
    # and the 15th 09:00 Melbourne row is the 14th 23:00 UTC.
    utc_groups = group_history(rows, by="day", tz=ZoneInfo("UTC"), now=now)
    assert [g["label"] for g in utc_groups] == ["Yesterday", "Mon 14 Sep"]
    assert utc_groups[0]["count"] == 3


def test_group_by_dashboard_keeps_row_order_and_folds_case() -> None:
    rows = [
        _row(30, target="Ambient"),
        _row(20, target="ambient", failed=True),
        _row(10, target="Weather"),
    ]
    groups = group_history(rows, by="dashboard", tz=MEL, now=100)
    assert [g["label"] for g in groups] == ["Ambient", "Weather"]
    assert groups[0]["count"] == 2 and groups[0]["failed"] == 1
    assert [r["timestamp"] for r in groups[0]["rows"]] == [30, 20]


def test_history_page_renders_day_groups_with_counts(app: Flask) -> None:
    log = app.config["EVENT_LOG"]
    now = datetime.now(MEL)
    today_noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if today_noon > now:
        today_noon = now
    # Keep both of today's rows on today's date: just after midnight a fixed
    # ten-minute gap would push the earlier row into yesterday.
    since_midnight = (
        today_noon - today_noon.replace(hour=0, minute=0, second=0, microsecond=0)
    ).total_seconds()
    gap = min(600, since_midnight / 2)
    # Oldest first: the log lists newest id first, as real pushes arrive.
    older = today_noon.timestamp() - 5 * 86400
    _record_at(log, older, target="weather")
    yesterday = today_noon.timestamp() - 86400
    _record_at(log, yesterday, target="weather")
    _record_at(
        log, today_noon.timestamp() - gap, target="home", status="failed", digest=None, error="boom"
    )
    _record_at(log, today_noon.timestamp(), target="home")

    client = app.test_client()
    _sign_in(client)
    html = client.get("/history").get_data(as_text=True)
    card = html[html.index("dx-hist-card") :]
    older_label = day_label(datetime.fromtimestamp(older, tz=MEL).date(), now.date())
    assert card.index("Today") < card.index("Yesterday") < card.index(older_label)
    today_block = card[card.index("Today") : card.index("Yesterday")]
    assert "2 pushes · 1 failed" in today_block
    assert today_block.count('data-history-row="') == 2
    assert "dx-hist-row--failed" in today_block
    assert 'class="dx-hist-dot is-bad"' in today_block
    yesterday_block = card[card.index("Yesterday") : card.index(older_label)]
    assert "1 push<" in yesterday_block and "failed" not in yesterday_block
    assert card.count("dx-hist-group-head") == 3


def test_history_page_by_dashboard_groups_by_target(app: Flask) -> None:
    log = app.config["EVENT_LOG"]
    base = datetime.now(MEL).timestamp()
    _record_at(log, base - 3 * 86400, target="weather")
    _record_at(log, base - 60, target="home")
    _record_at(log, base - 30, target="weather", status="failed", digest=None, error="boom")

    client = app.test_client()
    _sign_in(client)
    html = client.get("/history?sort=dashboard").get_data(as_text=True)
    card = html[html.index("dx-hist-card") :]
    assert card.count("dx-hist-group-head") == 2
    assert "Today" not in card and "Yesterday" not in card
    home = card[
        card.index('data-history-group="dash:home"') : card.index(
            'data-history-group="dash:weather"'
        )
    ]
    weather = card[card.index('data-history-group="dash:weather"') :]
    assert home.count('data-history-row="') == 1 and "1 push<" in home
    assert weather.count('data-history-row="') == 2 and "2 pushes · 1 failed" in weather
    # The toggle reads as selected and every other filter link keeps sort=dashboard.
    filters = html[: html.index("dx-hist-card")]
    by_dash = filters[filters.index("By dashboard") - 400 : filters.index("By dashboard")]
    assert "is-on" in by_dash
    assert filters.count("sort=dashboard") >= 3


def test_followups_fold_under_the_send_they_finish() -> None:
    from app.history_routes import attach_followups

    def row(i, ts, source, status="sent", target="Overhead", devices=("framed",)):
        return {
            "id": i,
            "ids": [i],
            "timestamp": ts,
            "time": f"t{i}",
            "source": source,
            "status": status,
            "target": target,
            "device_ids": list(devices),
            "failed": status not in ("sent", "no_change"),
        }

    rows = [  # newest first, as History lists them
        row(5, 1044.0, "follow-up"),
        row(4, 1035.0, "follow-up", "no_change"),
        row(3, 1030.0, "follow-up", target="LCARS"),
        row(2, 1027.0, "scheduler"),
        row(1, 900.0, "scheduler", target="LCARS"),
    ]
    out = attach_followups(rows)
    assert [r["id"] for r in out] == [2, 1], (
        "both follow-ups of the send fold under it; LCARS gets its own"
    )
    send = out[0]
    assert [f["id"] for f in send["followups"]] == [4, 5], "oldest first under the send"
    assert send["ids"] == [2, 4, 5], "deleting the send deletes the chain"
    assert send["finished_at"] == "t5" and send["finished_after_s"] == 17
    assert ([f["id"] for f in out[1]["followups"]] == [3] and "finished_at" not in out[1]) or out[
        1
    ]["finished_at"] == "t3"

    stray = attach_followups([row(9, 5000.0, "follow-up"), row(8, 100.0, "scheduler")])
    assert [r["id"] for r in stray] == [9, 8] and "followups" not in stray[1], (
        "too long after the send: shown on its own"
    )
    lone = attach_followups(
        [row(7, 100.0, "scheduler", "no_change"), row(6, 95.0, "follow-up", "no_change")]
    )
    assert [r["id"] for r in lone] == [7, 6], "a follow-up is never attached to a newer row"
