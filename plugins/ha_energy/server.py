"""ha_energy, solar / grid / battery / house-consumption snapshot.

Reads the same ``sensor.*`` entities you've already configured for HA's
Energy panel and produces the structured shape four selectable variants
paint:

    {
      "place": str, "time": str,
      "solar_w": float,           # current solar production, W
      "grid_w": float,            # current grid import (+) or export (-)
      "battery_w": float,         # battery charge (+) or discharge (-)
      "house_w": float,           # house consumption
      "battery_soc": float|None,  # 0-100 %, if entity provided
      "solar_today_kwh": float|None,
      "solar_yesterday_kwh": float|None,
      "flow": "solar" | "grid" | "battery" | "mixed",
      "sparkline_today": [...],   # today, local midnight to now, 48 half-hour slots
      "sparkline_yesterday": [...] # yesterday, midnight to midnight, 48 slots
    }

The widget batches a single ``get_states`` call to cover all four power
entities; the SoC + daily-energy entities are optional and only queried
when the user filled them in.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from flask import current_app

from app.tz_resolve import app_timezone


def _core() -> Any:
    return current_app.config["PLUGIN_REGISTRY"].get("ha_core").server_module


def choices(name: str) -> list[dict[str, str]]:
    """Entity picker for the six per-role selects. All energy/power
    sensors live under ``sensor.*`` in HA, so filter to that domain so
    the user isn't scrolling past every light and lock to find their
    inverter sensor."""
    core = _core()
    if name == "entity" and core is not None:
        return core.entity_choices(domains=("sensor",))
    return []


# -- helpers -----------------------------------------------------------


def _f(value: Any) -> float:
    """Coerce a state string to float. HA returns ``"unavailable"`` /
    ``"unknown"`` for offline entities; treat those as 0 so the widget
    keeps drawing instead of erroring."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _f_or_none(value: Any) -> float | None:
    """Same as ``_f`` but returns ``None`` for unparseable inputs, used
    for optional fields where ``0`` would be misleading (battery SoC)."""
    if value in (None, "", "unavailable", "unknown"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _state(states: list[dict[str, Any]], entity_id: str) -> dict[str, Any] | None:
    """Linear scan over a get_states() result. O(N) but the list is
    small (HA's full state dump for a typical install is a few hundred
    entries), and we do it once per render."""
    for st in states:
        if st.get("entity_id") == entity_id:
            return st
    return None


def _state_w(states: list[dict[str, Any]], entity_id: str) -> float:
    """Read the entity's ``state`` as a float interpreted as watts. If
    the entity's ``unit_of_measurement`` is kW, scale up by 1000 so the
    widget compares apples to apples regardless of which units the user
    has their power sensors reporting in."""
    if not entity_id:
        return 0.0
    st = _state(states, entity_id)
    if st is None:
        return 0.0
    value = _f(st.get("state"))
    unit = str((st.get("attributes") or {}).get("unit_of_measurement", "")).lower()
    if unit == "kw":
        value *= 1000
    return value


def _dominant_flow(solar: float, grid: float, battery: float, house: float) -> str:
    """Which contribution is currently the largest source of house
    power? Used by some variants to highlight a single primary accent."""
    contributions = {
        "solar": max(0.0, solar),
        "grid": max(0.0, grid),
        # Battery discharge is positive contribution; charging is negative.
        "battery": max(0.0, -battery) if battery < 0 else 0.0,
    }
    if not any(contributions.values()):
        return "mixed"
    return max(contributions, key=lambda k: contributions[k])


# -- entry point -------------------------------------------------------


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings, ctx
    core = _core()
    try:
        states = core.get_states()
    except Exception as err:
        return {"error": core.coerce_error(err)}

    solar = _state_w(states, options.get("solar_entity") or "")
    grid = _state_w(states, options.get("grid_entity") or "")
    battery = _state_w(states, options.get("battery_entity") or "")
    house = _state_w(states, options.get("house_entity") or "")

    soc_entity = (options.get("battery_soc_entity") or "").strip()
    soc = None
    if soc_entity:
        soc_state = _state(states, soc_entity)
        if soc_state is not None:
            soc = _f_or_none(soc_state.get("state"))

    today_entity = (options.get("solar_today_entity") or "").strip()
    solar_today_kwh = None
    solar_yesterday_kwh = None
    if today_entity:
        today_state = _state(states, today_entity)
        if today_state is not None:
            solar_today_kwh = _f_or_none(today_state.get("state"))

    # Today-vs-yesterday sparkline: two calendar days in the panel's
    # timezone, each binned into 48 half-hour slots by clock time, so the
    # solid and the dashed line share an x-axis that runs midnight to
    # midnight. Prefer the solar entity (it has a clear day-shaped curve);
    # fall back to house consumption when solar's not configured.
    now = datetime.now(app_timezone())
    if today_entity and options.get("show_solar_yesterday") is True:
        try:
            energy_samples = core.history(today_entity, hours=48)
        except Exception:
            energy_samples = []
        yesterday_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        previous_day_samples: list[tuple[datetime, float]] = []
        for sample in energy_samples:
            sample_dt = _parse_dt(sample.get("last_changed") or sample.get("last_updated"))
            value = _f_or_none(sample.get("state"))
            if sample_dt is None or value is None:
                continue
            local_dt = sample_dt.astimezone(now.tzinfo)
            if yesterday_start <= local_dt < today_start:
                previous_day_samples.append((local_dt, value))
        if previous_day_samples:
            solar_yesterday_kwh = round(max(previous_day_samples, key=lambda item: item[0])[1], 2)

    spark_entity = (options.get("solar_entity") or options.get("house_entity") or "").strip()
    sparkline_today: list[float | None] = []
    sparkline_yesterday: list[float | None] = []
    if spark_entity:
        try:
            samples = core.history(spark_entity, hours=48)
        except Exception:
            samples = []
        if samples:
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            yesterday_start = today_start - timedelta(days=1)
            sparkline_yesterday = _bin_day(samples, yesterday_start, today_start, slots=48)
            sparkline_today = _bin_day(samples, today_start, now, slots=48)

    flow = _dominant_flow(solar, grid, battery, house)

    flow = _dominant_flow(solar, grid, battery, house)

    return {
        "label": options.get("label", "Home"),
        "place": options.get("label", "Home"),
        "time": now.strftime("%H:%M"),
        "hour": now.hour,
        "solar_w": round(solar, 1),
        "grid_w": round(grid, 1),
        "battery_w": round(battery, 1),
        "house_w": round(house, 1),
        "battery_soc": round(soc, 1) if soc is not None else None,
        "solar_today_kwh": round(solar_today_kwh, 2) if solar_today_kwh is not None else None,
        "solar_yesterday_kwh": solar_yesterday_kwh,
        "flow": flow,
        # Backwards-compat: `sparkline` mirrors today's series without the
        # trailing gap, for anything that still reads the old key.
        "sparkline": [v for v in sparkline_today if v is not None],
        "sparkline_today": sparkline_today,
        "sparkline_yesterday": sparkline_yesterday,
        # Bookkeeping for the cache layer.
        "_fetched_at": int(time.time()),
    }


def _parse_dt(raw: Any) -> datetime | None:
    """A HA history timestamp as an aware datetime, or None. HA sends UTC
    ISO strings; a naive one is read as UTC rather than guessed at."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _bin_day(
    samples: list[dict[str, Any]], start: datetime, end: datetime, *, slots: int
) -> list[float | None]:
    """Bin a history into ``slots`` equal clock-time slots between two
    local instants, and mark the slots after ``end`` as None.

    HA's history endpoint returns one sample per state change, so a solar
    sensor produces hundreds of samples while the sun is up and none all
    night. Binning by sample count (what this widget did up to 0.432.3)
    squeezed the night to nothing and put yesterday's peak after sunset
    (#339). Each slot here covers the same span of clock time: the mean
    of the samples that fall in it, or the last value seen before it,
    since a state holds until the next change. Slots after ``end`` (the
    rest of today) are None so the line stops at now."""
    if slots < 1 or end <= start:
        return []
    day_len = timedelta(days=1)
    slot_len = day_len / slots
    buckets: list[list[float]] = [[] for _ in range(slots)]
    before: tuple[datetime, float] | None = None
    for s in samples:
        dt = _parse_dt(s.get("last_changed") or s.get("last_updated"))
        if dt is None:
            continue
        value = _f(s.get("state"))
        if dt < start:
            if before is None or dt >= before[0]:
                before = (dt, value)
            continue
        if dt >= start + day_len:
            continue
        idx = min(slots - 1, int((dt - start) / slot_len))
        buckets[idx].append(value)
    last_open = min(slots - 1, int((end - start) / slot_len))
    out: list[float | None] = []
    carry: float | None = before[1] if before else None
    for i, bucket in enumerate(buckets):
        if i > last_open:
            out.append(None)
            continue
        if bucket:
            carry = round(sum(bucket) / len(bucket), 1)
        out.append(round(carry, 1) if carry is not None else None)
    return out
