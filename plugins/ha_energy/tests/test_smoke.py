"""Smoke tests for the Home Assistant Energy widget."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

_SERVER_PATH = Path(__file__).resolve().parent.parent / "server.py"


def _load_server() -> Any:
    """Load this plugin's ``server.py`` under a unique module name.

    Every plugin ships a module called ``server``; a bare ``import server``
    here would hand back whichever plugin's module a sibling suite imported
    first (pytest collects ``plugins/`` alphabetically, so calendar_day's).
    """
    spec = importlib.util.spec_from_file_location("ha_energy_server_under_test", _SERVER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = _load_server()


def test_widget_clock_uses_app_timezone() -> None:
    zone = ZoneInfo("Europe/Berlin")

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is zone
            return cls(2026, 9, 28, 20, 5, tzinfo=tz)

    core = MagicMock()
    core.get_states.return_value = []
    registry = MagicMock()
    registry.get.return_value = SimpleNamespace(server_module=core)
    app = SimpleNamespace(config={"PLUGIN_REGISTRY": registry})

    with (
        patch.object(server, "current_app", app),
        patch.object(server, "datetime", FixedDateTime),
        patch.object(server, "app_timezone", return_value=zone),
    ):
        result = server.fetch(options={}, settings={}, ctx={})

    assert result["time"] == "20:05"
    assert result["hour"] == 20


def _sample(when: datetime, state: str) -> dict[str, Any]:
    return {"state": state, "last_changed": when.astimezone(ZoneInfo("UTC")).isoformat()}


def _fetch_with_history(samples: list[dict[str, Any]], zone: ZoneInfo, now: datetime) -> Any:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    core = MagicMock()
    core.get_states.return_value = []
    core.history.return_value = samples
    registry = MagicMock()
    registry.get.return_value = SimpleNamespace(server_module=core)
    app = SimpleNamespace(config={"PLUGIN_REGISTRY": registry})
    with (
        patch.object(server, "current_app", app),
        patch.object(server, "datetime", FixedDateTime),
        patch.object(server, "app_timezone", return_value=zone),
    ):
        result = server.fetch(options={"solar_entity": "sensor.solar"}, settings={}, ctx={})
    core.history.assert_called_once_with("sensor.solar", hours=48)
    return result


def test_sparkline_bins_by_clock_time_over_calendar_days() -> None:
    """Both lines are calendar days in the panel's timezone, 48 half-hour
    slots each, binned by timestamp rather than sample count (#339). A
    solar sensor that sits at 0 W all night has almost no night samples,
    so binning by count squeezed the night away and put yesterday's peak
    after sunset."""
    zone = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 9, 28, 20, 5, tzinfo=zone)
    today = now.replace(hour=0, minute=0)
    yesterday = today - timedelta(days=1)
    samples = [
        # HA opens the window with the state at start time.
        _sample(yesterday - timedelta(hours=4), "0"),
        # Yesterday: a burst of daytime samples around noon, nothing at night.
        _sample(yesterday + timedelta(hours=11, minutes=50), "1000"),
        _sample(yesterday + timedelta(hours=12, minutes=10), "3000"),
        _sample(yesterday + timedelta(hours=12, minutes=40), "2000"),
        _sample(yesterday + timedelta(hours=18), "0"),
        # Today: sun up at 07:00, a peak, down again at 18:30.
        _sample(today + timedelta(hours=7), "500"),
        _sample(today + timedelta(hours=13), "2500"),
        _sample(today + timedelta(hours=18, minutes=30), "0"),
    ]
    result = _fetch_with_history(samples, zone, now)

    y = result["sparkline_yesterday"]
    t = result["sparkline_today"]
    assert len(y) == 48 and len(t) == 48

    # Yesterday: night holds the 0 W seen before midnight, the noon slots
    # carry the samples that fall in them, and the evening holds 0 again.
    assert y[0] == 0.0 and y[10] == 0.0
    assert y[23] == 1000.0  # 11:30-12:00
    assert y[24] == 3000.0  # 12:00-12:30
    assert y[25] == 2000.0  # 12:30-13:00
    assert y[26] == 2000.0  # 13:00-13:30 holds the last value
    assert y[36] == 0.0 and y[47] == 0.0

    # Today: 0 W carried over from yesterday evening until 07:00, then
    # the day's values, and None after the current 20:00-20:30 slot so
    # the line stops at now.
    assert t[0] == 0.0 and t[13] == 0.0
    assert t[14] == 500.0  # 07:00
    assert t[26] == 2500.0  # 13:00
    assert t[37] == 0.0  # 18:30
    assert t[40] == 0.0  # 20:00-20:30 is the current slot
    assert t[41:] == [None] * 7
    assert result["sparkline"] == t[:41]


def test_sparkline_yesterday_is_empty_without_samples() -> None:
    zone = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 9, 28, 20, 5, tzinfo=zone)
    result = _fetch_with_history([], zone, now)
    assert result["sparkline_today"] == []
    assert result["sparkline_yesterday"] == []
    assert result["sparkline"] == []


def test_yesterday_solar_total_uses_latest_local_day_sample() -> None:
    zone = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 9, 28, 20, 5, tzinfo=zone)
    yesterday = now.replace(hour=0, minute=0) - timedelta(days=1)
    samples = [
        _sample(yesterday - timedelta(minutes=1), "7.4"),
        _sample(yesterday + timedelta(hours=12), "10.25"),
        _sample(yesterday + timedelta(hours=23, minutes=59), "14.8"),
        _sample(yesterday + timedelta(days=1), "0"),
        _sample(yesterday + timedelta(hours=23, minutes=58), "unavailable"),
    ]

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    core = MagicMock()
    core.get_states.return_value = [
        {"entity_id": "sensor.solar_today", "state": "8.6"},
    ]
    core.history.return_value = samples
    registry = MagicMock()
    registry.get.return_value = SimpleNamespace(server_module=core)
    app = SimpleNamespace(config={"PLUGIN_REGISTRY": registry})

    with (
        patch.object(server, "current_app", app),
        patch.object(server, "datetime", FixedDateTime),
        patch.object(server, "app_timezone", return_value=zone),
    ):
        result = server.fetch(
            options={"solar_today_entity": "sensor.solar_today", "show_solar_yesterday": True},
            settings={},
            ctx={},
        )

    core.history.assert_called_once_with("sensor.solar_today", hours=48)
    assert result["solar_today_kwh"] == 8.6
    assert result["solar_yesterday_kwh"] == 14.8


def test_yesterday_solar_total_respects_toggle_and_missing_history() -> None:
    zone = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 9, 28, 20, 5, tzinfo=zone)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    core = MagicMock()
    core.get_states.return_value = []
    registry = MagicMock()
    registry.get.return_value = SimpleNamespace(server_module=core)
    app = SimpleNamespace(config={"PLUGIN_REGISTRY": registry})
    with (
        patch.object(server, "current_app", app),
        patch.object(server, "datetime", FixedDateTime),
        patch.object(server, "app_timezone", return_value=zone),
    ):
        result = server.fetch(
            options={"solar_today_entity": "sensor.solar_today", "show_solar_yesterday": True},
            settings={},
            ctx={},
        )
        core.history.assert_called_once_with("sensor.solar_today", hours=48)
        assert result["solar_yesterday_kwh"] is None

        core.history.reset_mock()
        result = server.fetch(
            options={"solar_today_entity": "sensor.solar_today"},
            settings={},
            ctx={},
        )

    core.history.assert_not_called()
    assert result["solar_yesterday_kwh"] is None
