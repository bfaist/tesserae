"""Bloomin8 Canvas Schedule Pull API (issue #344).

A Bloomin8 Canvas is told an upstream URL, an access token and a first
wake time through its own ``PUT /upstream/pull_settings``. From then on
it wakes at the time it was last given, calls ``{upstream}/eink_pull``,
paints the image it is pointed at, reports the result to
``{upstream}/eink_signal`` and sleeps until the ``next_cron_time`` in the
``/eink_pull`` reply. Setting the upstream to ``<tesserae>/api/bloomin8``
puts all three routes here:

* ``GET /api/bloomin8/eink_pull``: identifies the device by its
  ``X-Access-Token``, records the battery reading as a heartbeat, and
  answers with the latest frame's URL and the next wake. The wake comes
  from ``app.device_poll``, the same decision the REST and TRMNL paths
  use, so a scheduled page change pulls it earlier. A frame the Canvas
  already confirmed painting is answered with ``status: 204`` so it goes
  back to sleep without repainting the same picture.

* ``GET /api/bloomin8/eink_signal``: the Canvas's confirmation after it
  decoded and painted a frame. Marks that frame as on the glass.

* ``GET /api/bloomin8/frame/<device_id>/<digest>_<P|L>.jpg``: the frame
  itself, built from the composition PNG.

The Canvas's HTTP client is strict, and every reply here follows
Bloomin8's notes: the control endpoints always answer ``HTTP 200`` with
the real outcome in the JSON ``status``, every reply carries a future
``next_cron_time`` in UTC ``Z`` form (a missing or past one strands the
device or busy-loops it), and the image is a baseline JPEG stored in
portrait pixel order with an exact ``Content-Length``, no chunking, no
compression and no redirect. A landscape composition is turned 90°
clockwise and named ``_L.jpg``; the Canvas turns it back when it paints.

The frame is the full-colour composition, not a quantised artifact: the
Canvas dithers for its own panel, and JPEG over a pre-dithered image
would smear the dots.
"""

from __future__ import annotations

import io
import json
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, Flask, Response, abort, current_app, jsonify, request

from app import device_poll
from app.device_loader import Device

logger = logging.getLogger(__name__)

bp = Blueprint("bloomin8_api", __name__)

KIND_ID = "bloomin8_canvas"

# Bloomin8 asks for at least a minute between wakes: a wake, download and
# paint takes 10-30 s, so anything tighter overruns.
MIN_WAKE_S = 60
# Wake again in an hour after a pull that couldn't be served (unknown
# token, nothing rendered yet). A null next_cron_time would stop the
# Canvas pulling until it is re-armed by hand, which is the wrong answer
# to a token typo or a fresh install.
RETRY_WAKE_S = 3600
# Canvas 13.3" native buffer, used when the panel block carries none.
DEFAULT_NATIVE = (1200, 1600)
JPEG_QUALITY = 90

_DIGEST_RE = re.compile(r"^[0-9a-f]{16,64}$")
_DEVICE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
# Frames each device may fetch: the ones /eink_pull handed out recently.
_SERVED_PER_DEVICE = 4
_PENDING_PULLS = 64
_JPEG_CACHE = 8


class _State:
    """Per-app memory of what each Canvas was pointed at and painted.

    Held in memory only. After a restart the first pull repaints the
    current frame once, which is harmless."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        # device_id -> digests handed out by /eink_pull, newest last.
        self.served: dict[str, list[str]] = {}
        # device_id -> digest the Canvas confirmed painting.
        self.shown: dict[str, str] = {}
        # pull_id -> (device_id, digest) awaiting /eink_signal.
        self.pending: OrderedDict[str, tuple[str, str]] = OrderedDict()
        # (digest, w, h) -> (jpeg bytes, suffix).
        self.jpegs: OrderedDict[tuple[str, int, int], tuple[bytes, str]] = OrderedDict()


def _state() -> _State:
    return current_app.config["BLOOMIN8_STATE"]  # type: ignore[no-any-return]


# -- identity -----------------------------------------------------------


def _device_by_token(token: str) -> Device | None:
    """The Canvas instance whose ``access_token`` matches, scanning every
    instance so the comparison time doesn't depend on where it matched."""
    if not token:
        return None
    matched: Device | None = None
    for dev in current_app.config["DEVICE_REGISTRY"].all():
        if dev.kind_of != KIND_ID:
            continue
        stored = dev.manifest.get("access_token")
        if isinstance(stored, str) and stored and secrets.compare_digest(stored, token):
            matched = dev
    return matched


def _request_token() -> str:
    return (request.headers.get("X-Access-Token") or "").strip()


# -- timing -------------------------------------------------------------


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _configured_wake_s(device: Device) -> int:
    """The device's wake interval: an always-on cadence if it has one,
    else the stored ``sleep_interval_s``, else the kind default."""
    awake = device_poll.device_awake_poll_s(device)
    if awake is not None:
        return max(MIN_WAKE_S, awake)
    section = current_app.config["SETTINGS_STORE"].get_section("devices") or {}
    stored = section.get(device.id) if isinstance(section, dict) else None
    if isinstance(stored, dict) and isinstance(stored.get("sleep_interval_s"), int):
        return max(MIN_WAKE_S, int(stored["sleep_interval_s"]))
    spec = (device.config_schema or {}).get("sleep_interval_s")
    if isinstance(spec, dict) and isinstance(spec.get("default"), int):
        return max(MIN_WAKE_S, int(spec["default"]))
    return 1800


def _next_wake_epoch(device: Device) -> float:
    """When the Canvas should wake next, as an absolute instant at least
    ``MIN_WAKE_S`` away. Any fault falls back to the configured interval."""
    now = time.time()
    configured = _configured_wake_s(device)
    try:
        rel, wake_at = device_poll.next_poll_decision(device, configured_s=configured)
        target = float(wake_at) if wake_at else now + rel
    except Exception:
        logger.exception("bloomin8: next wake failed for device=%s", device.id)
        target = now + configured
    return max(target, now + MIN_WAKE_S)


def _reply(status: int, message: str, next_epoch: float, **data: Any) -> Response:
    """Bloomin8 reply envelope: always HTTP 200, outcome in ``status``."""
    body: dict[str, Any] = {
        "status": status,
        "message": message,
        "data": {"next_cron_time": _iso_utc(next_epoch), **data},
    }
    if status == 200 and "image_url" in data:
        body["type"] = "SHOW"
    return jsonify(body)


# -- status -------------------------------------------------------------


def _record_heartbeat(device: Device) -> None:
    """Feed the pull's query parameters through the shared heartbeat path
    so last seen, battery history and HA sensors update like any other
    device. Best-effort: a failure here must never cost the Canvas its
    frame."""
    from app.transport_wiring import record_status_heartbeat

    events = current_app.config.get("EVENT_LOG")
    if events is None:
        return
    payload = json.dumps(
        {"battery": request.args.get("battery"), "device_id": request.args.get("device_id")}
    ).encode("utf-8")
    try:
        record_status_heartbeat(
            app=current_app._get_current_object(),  # type: ignore[attr-defined]
            device=device,
            payload=payload,
            status_cache=current_app.config["DEVICE_STATUS"],
            event_log=events,
            event_target=f"bloomin8://{device.id}/pull",
        )
    except Exception:
        logger.exception("bloomin8: heartbeat failed for device=%s", device.id)


# -- frames -------------------------------------------------------------


def _renders_dir() -> Path:
    return Path(current_app.config["RENDERS_DIR"])


def _native_dims(device: Device) -> tuple[int, int]:
    """The Canvas's portrait buffer. Stored in portrait order whatever the
    panel block says, since that is how the Canvas expects every JPEG."""
    panel = device.panel or {}
    try:
        w, h = int(panel["native_w"]), int(panel["native_h"])
    except (KeyError, TypeError, ValueError):
        w, h = DEFAULT_NATIVE
    if w <= 0 or h <= 0:
        w, h = DEFAULT_NATIVE
    return (min(w, h), max(w, h))


def _build_jpeg(src: Path, native: tuple[int, int]) -> tuple[bytes, str]:
    """Baseline JPEG of the composition in portrait pixel order, sized to
    the native buffer, plus the orientation suffix (``P`` or ``L``)."""
    from PIL import Image, ImageOps

    with Image.open(src) as opened:
        img = opened.convert("RGB")
    suffix = "P"
    if img.width > img.height:
        # Landscape content: stored turned 90° clockwise, the Canvas turns
        # it back counter-clockwise when it paints an _L file.
        img = img.transpose(Image.Transpose.ROTATE_270)
        suffix = "L"
    if img.size != native:
        img = ImageOps.pad(img, native, color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, subsampling=0, progressive=False)
    return buf.getvalue(), suffix


def _jpeg_for(device: Device, digest: str) -> tuple[bytes, str] | None:
    state = _state()
    native = _native_dims(device)
    key = (digest, native[0], native[1])
    with state.lock:
        hit = state.jpegs.get(key)
        if hit is not None:
            state.jpegs.move_to_end(key)
            return hit
    src = _renders_dir() / f"{digest}.png"
    if not src.is_file():
        return None
    built = _build_jpeg(src, native)
    with state.lock:
        state.jpegs[key] = built
        while len(state.jpegs) > _JPEG_CACHE:
            state.jpegs.popitem(last=False)
    return built


def _current_digest(device: Device) -> str | None:
    push_mgr = current_app.config.get("PUSH_MANAGER")
    latest = push_mgr.latest_render_for(device.id) if push_mgr is not None else None
    if not latest:
        return None
    digest = latest.get("composition_digest")
    return digest if isinstance(digest, str) and _DIGEST_RE.match(digest) else None


# -- routes -------------------------------------------------------------


@bp.get("/api/bloomin8/eink_pull")
def eink_pull() -> Response:
    device = _device_by_token(_request_token())
    if device is None:
        logger.info(
            "bloomin8: rejected /eink_pull from %s (hw device_id=%r)",
            request.remote_addr,
            request.args.get("device_id"),
        )
        return _reply(
            401,
            "Unknown access token. Copy the token from the device card in Tesserae.",
            time.time() + RETRY_WAKE_S,
        )

    _record_heartbeat(device)
    next_epoch = _next_wake_epoch(device)
    digest = _current_digest(device)
    if digest is None:
        return _reply(204, "Nothing rendered for this device yet", next_epoch)

    state = _state()
    with state.lock:
        already_shown = state.shown.get(device.id) == digest
    if already_shown:
        logger.info(
            "bloomin8: device=%s already shows %s, next wake %s",
            device.id,
            digest[:12],
            _iso_utc(next_epoch),
        )
        return _reply(204, "No new image", next_epoch)

    try:
        built = _jpeg_for(device, digest)
    except Exception:
        logger.exception("bloomin8: JPEG build failed for device=%s", device.id)
        built = None
    if built is None:
        return _reply(204, "Frame unavailable", next_epoch)
    _, suffix = built

    pull_id = (request.args.get("pull_id") or "").strip()[:64]
    with state.lock:
        served = state.served.setdefault(device.id, [])
        if digest in served:
            served.remove(digest)
        served.append(digest)
        del served[:-_SERVED_PER_DEVICE]
        if pull_id:
            state.pending[pull_id] = (device.id, digest)
            while len(state.pending) > _PENDING_PULLS:
                state.pending.popitem(last=False)

    image_url = (
        f"{request.url_root.rstrip('/')}/api/bloomin8/frame/{device.id}/{digest}_{suffix}.jpg"
    )
    logger.info(
        "bloomin8: served /eink_pull to device=%s frame=%s_%s next wake %s",
        device.id,
        digest[:12],
        suffix,
        _iso_utc(next_epoch),
    )
    return _reply(200, "Image ready", next_epoch, image_url=image_url)


@bp.get("/api/bloomin8/eink_signal")
def eink_signal() -> Response:
    """The Canvas reports a paint. Only a matching token can mark a frame
    as shown; anything else is acknowledged and ignored."""
    device = _device_by_token(_request_token())
    pull_id = (request.args.get("pull_id") or "").strip()
    success = request.args.get("success") == "1"
    state = _state()
    with state.lock:
        entry = state.pending.pop(pull_id, None)
        if device is not None and entry is not None and entry[0] == device.id and success:
            state.shown[device.id] = entry[1]
    if device is not None:
        logger.info(
            "bloomin8: device=%s signal pull=%s success=%s",
            device.id,
            pull_id[:12],
            int(success),
        )
    return jsonify({"status": 200, "message": "Feedback recorded"})


@bp.get("/api/bloomin8/frame/<device_id>/<name>")
def frame_jpeg(device_id: str, name: str) -> Response:
    """The JPEG a pull pointed at. Only frames recently handed to this
    device are served: the route sits outside the session gate (the
    Canvas sends no token on image fetches), so it must not become a way
    to read any composition by digest."""
    match = re.fullmatch(r"([0-9a-f]{16,64})_([PL])\.jpg", name)
    if not _DEVICE_ID_RE.match(device_id) or match is None:
        abort(404)
    digest = match.group(1)
    device = current_app.config["DEVICE_REGISTRY"].devices.get(device_id)
    if device is None or device.kind_of != KIND_ID:
        abort(404)
    state = _state()
    with state.lock:
        allowed = digest in state.served.get(device_id, [])
    if not allowed:
        abort(404)
    built = _jpeg_for(device, digest)
    if built is None:
        abort(404)
    body, _ = built
    # Explicit length, no chunking, no encoding: the Canvas sizes its
    # buffer from Content-Length and decompresses nothing.
    return Response(
        body,
        status=200,
        headers={
            "Content-Type": "image/jpeg",
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
        },
    )


def register(app: Flask) -> None:
    app.config["BLOOMIN8_STATE"] = _State()
    app.register_blueprint(bp)
