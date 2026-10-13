"""bloomin8_canvas device contract.

A Bloomin8 Canvas pulls its frames through Bloomin8's Schedule Pull API:
it wakes at the time it was last told, calls ``GET {upstream}/eink_pull``
with ``device_id``, ``pull_id``, ``cron_time`` and ``battery`` in the
query string and the access token in ``X-Access-Token``, paints the JPEG
it is pointed at, and sleeps until the ``next_cron_time`` in the reply.
``app.bloomin8_api`` backs those endpoints.

Status reaches Tesserae through that query string rather than a separate
heartbeat. ``app.bloomin8_api`` hands the query parameters over as a JSON
object and ``parse_status`` below maps them onto the same status shape the
other kinds produce, so the device card, battery history and Home
Assistant sensors work the same way.

There is no ``config_topic``: the wake interval reaches the Canvas as the
``next_cron_time`` in each ``/eink_pull`` reply.
"""

from __future__ import annotations

import json
from typing import Any

# Bounds match config_schema in device.json. Bloomin8 asks for at least a
# minute between wakes (a wake, download and paint takes 10-30 s).
SLEEP_INTERVAL_MIN_S = 60
SLEEP_INTERVAL_MAX_S = 7 * 24 * 60 * 60


def parse_status(payload: bytes) -> dict[str, Any]:
    """Normalise an ``/eink_pull`` request's query parameters into the
    common status shape. ``battery`` is a percentage; ``device_id`` is
    the Canvas's own hardware id, kept as ``hw_id`` so it shows on the
    device card without being mistaken for the Tesserae device id."""
    out: dict[str, Any] = {"battery_pct": None, "hw_id": None}
    if not payload:
        return out
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        out["error"] = "status payload was not JSON"
        return out
    if not isinstance(decoded, dict):
        out["error"] = f"expected JSON object, got {type(decoded).__name__}"
        return out
    try:
        pct = int(decoded.get("battery"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pct = None
    if pct is not None and 0 <= pct <= 100:
        out["battery_pct"] = pct
    hw_id = decoded.get("device_id")
    if isinstance(hw_id, str) and hw_id.strip():
        out["hw_id"] = hw_id.strip()[:64]
    return out


def validate_config(payload: dict[str, Any]) -> tuple[bool, str | None]:
    """Check the wake interval before it is used to schedule the Canvas."""
    if "sleep_interval_s" not in payload:
        return False, "missing 'sleep_interval_s'"
    try:
        value = int(payload["sleep_interval_s"])
    except (TypeError, ValueError):
        return False, "sleep_interval_s must be an integer"
    if value < SLEEP_INTERVAL_MIN_S:
        return False, f"sleep_interval_s must be >= {SLEEP_INTERVAL_MIN_S} (got {value})"
    if value > SLEEP_INTERVAL_MAX_S:
        return False, f"sleep_interval_s must be <= {SLEEP_INTERVAL_MAX_S} (got {value})"
    return True, None
