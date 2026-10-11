"""Touch-v3 glyph atlas builder + /frame/spec attachment + /atlas serve.

Uses a fake rasterizer (a plain grayscale strip + per-glyph boxes) so no headless
browser is needed; the byte content is irrelevant to the descriptor shape.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from jsonschema import Draft202012Validator
from PIL import Image

import app.rest_api as rest_api
from app.main import REPO_ROOT, create_app
from app.state.page_store import Page
from app.state.panel_store import CanvasLayout, Element
from app.touch_atlas import build_touch_atlas

_SCHEMA_DIR = REPO_ROOT / "schema"


def _fake_rasterize(px: int, weight: int, charset: str) -> tuple[bytes, list[dict[str, Any]]]:
    cell = max(4, px // 2)
    img = Image.new("L", (cell * len(charset) + 2, px + 4), 255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    boxes = [{"ch": ch, "x": i * cell, "y": 2, "w": cell, "h": px} for i, ch in enumerate(charset)]
    return buf.getvalue(), boxes


# --- unit: the builder ---


def test_build_touch_atlas_descriptor(tmp_path: Path) -> None:
    desc = build_touch_atlas("l20", renders_dir=tmp_path, rasterize=_fake_rasterize)
    assert desc is not None
    assert desc["id"] == "l20" and desc["px"] == 20 and desc["weight"] == 400
    assert desc["format"] == "gray4"
    assert desc["ascent"] + desc["descent"] == desc["strip_h"]
    for ch in "AZaz09.%":
        g = desc["glyphs"][ch]
        assert g["adv"] == g["w"]  # packed cell is advance-wide
    assert (tmp_path / f"touch-atlas-{desc['digest']}.bin").is_file()


def _ink_rasterize(px: int, weight: int, charset: str) -> tuple[bytes, list[dict[str, Any]]]:
    """Paper (255) strip whose glyph cells each carry one black (0) column."""
    cell = max(4, px // 2)
    img = Image.new("L", (cell * len(charset) + 2, px + 4), 255)
    for i in range(len(charset)):
        for y in range(2, px + 2):
            img.putpixel((i * cell + 1, y), 0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    boxes = [{"ch": ch, "x": i * cell, "y": 2, "w": cell, "h": px} for i, ch in enumerate(charset)]
    return buf.getvalue(), boxes


def test_touch_atlas_is_ink_scale_overlay_stays_luminance(tmp_path: Path) -> None:
    """touch-v3 reads 0 as paper and 15 as full ink (schema/atlas.schema.json);
    the overlay path copies nibbles as luminance. Packing touch strips as
    luminance drew every label as white text in a black box (#360)."""
    from app.overlay_sync import build_atlas

    desc = build_touch_atlas("l20", renders_dir=tmp_path, rasterize=_ink_rasterize)
    assert desc is not None
    touch = (tmp_path / f"touch-atlas-{desc['digest']}.bin").read_bytes()
    g = desc["glyphs"]["A"]
    # Row 0 of the strip: byte for pixels (x, x+1) of glyph A's cell, whose
    # column x+1 is the black stroke.
    b = touch[g["x"] // 2]
    assert (b >> 4, b & 0x0F) == (0x0, 0xF)  # paper -> 0, ink -> 15

    over = build_atlas(20, 400, renders_dir=tmp_path, rasterize=_ink_rasterize)
    assert over is not None
    lum = (tmp_path / f"overlay-atlas-{over['digest']}.bin").read_bytes()
    first = next(iter(over["glyphs"].values()))  # the overlay charset is values only
    b = lum[first["x"] // 2]
    assert (b >> 4, b & 0x0F) == (0xF, 0x0)  # white -> 15, black -> 0


def test_cached_atlas_from_an_older_build_is_rebuilt(tmp_path: Path) -> None:
    """A meta sidecar written before the build tag (an inverted touch strip on
    a server that had it cached) is rebuilt, and the strip it named goes."""
    desc = build_touch_atlas("l20", renders_dir=tmp_path, rasterize=_ink_rasterize)
    assert desc is not None
    meta_path = tmp_path / "touch-atlas-20-400.json"
    meta = json.loads(meta_path.read_text())
    stale = "00000000deadbeef"
    (tmp_path / f"touch-atlas-{stale}.bin").write_bytes(b"old")
    meta.pop("build")
    meta["digest"] = stale
    meta_path.write_text(json.dumps(meta))
    # Cache-only refuses the stale entry rather than serving it.
    assert build_touch_atlas("l20", renders_dir=tmp_path, rasterize=None) is None
    again = build_touch_atlas("l20", renders_dir=tmp_path, rasterize=_ink_rasterize)
    assert again is not None and again["digest"] == desc["digest"]
    assert not (tmp_path / f"touch-atlas-{stale}.bin").exists()
    assert build_touch_atlas("l20", renders_dir=tmp_path, rasterize=None) is not None


def test_atlas_warm_shares_one_guard(app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup, post-render and poll-time warms share one de-dup guard, and a
    queued job lands in the cache."""
    import threading

    from app import atlas_warm

    started = threading.Event()
    release = threading.Event()
    calls: list[tuple[int, int]] = []

    def slow(px: int, weight: int, charset: str) -> tuple[bytes, list[dict[str, Any]]]:
        calls.append((px, weight))
        started.set()
        release.wait(5)
        return _fake_rasterize(px, weight, charset)

    app.config["OVERLAY_ATLAS_RASTERIZER"] = slow
    atlas_warm.schedule(app, [("touch", "l20")], "http://localhost/")
    assert started.wait(5)
    atlas_warm.schedule(app, [("touch", "l20")], "http://localhost/")  # in flight: skipped
    release.set()
    for _ in range(100):
        if not atlas_warm._warming:
            break
        threading.Event().wait(0.05)
    assert calls == [(20, 400)]
    rd = Path(app.config["RENDERS_DIR"])
    assert build_touch_atlas("l20", renders_dir=rd, rasterize=None) is not None


def test_overlay_jobs_from_slots() -> None:
    from app.atlas_warm import overlay_jobs

    slots = [{"px": 32, "weight": 700}, {"px": "18", "weight": 400}, {"px": 9}, "junk"]
    assert overlay_jobs(slots) == [("overlay", 32, 700), ("overlay", 18, 400)]


def test_unknown_role_is_none(tmp_path: Path) -> None:
    assert build_touch_atlas("zzz", renders_dir=tmp_path, rasterize=_fake_rasterize) is None


# --- endpoint: attach on /frame/spec + serve on /atlas ---


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
    a.config["OVERLAY_ATLAS_RASTERIZER"] = _fake_rasterize
    Path(a.config["RENDERS_DIR"]).mkdir(parents=True, exist_ok=True)
    return a


def _register(app: Flask, client) -> str:
    client.post("/setup", data={"password": "abcdefgh", "password_confirm": "abcdefgh"})
    code = app.config["PAIRING_STORE"].issue(note="test").code
    resp = client.post(
        "/api/v1/device/register",
        headers={"X-Pairing-Code": code, "Content-Type": "application/json"},
        data=json.dumps(
            {"device_id": "e1003", "kind": "esp32_client", "panel_w": 1872, "panel_h": 1404}
        ),
    )
    assert resp.status_code == 201
    return resp.get_json()["device_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed(app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    app.config["PAGE_STORE"].save(
        Page(
            id="p1",
            name="Touch",
            layout_kind="canvas",
            canvas=CanvasLayout(
                els=[
                    Element(
                        id="sw",
                        kind="switch",
                        x=10,
                        y=10,
                        w=160,
                        h=80,
                        label="Desk",
                        value_key="ha:light.desk",
                    ),
                    Element(
                        id="sl",
                        kind="slider",
                        x=10,
                        y=120,
                        w=300,
                        h=70,
                        axis="x",
                        value_min=0,
                        value_max=100,
                    ),
                ]
            ),
        )
    )
    monkeypatch.setattr(
        app.config["PUSH_MANAGER"], "latest_render_for", lambda _d: {"digest": "a" * 16}
    )
    monkeypatch.setattr(
        rest_api,
        "_frame_info_for_digest",
        lambda _dev, _dig: {"page_id": "p1", "composition_digest": "c"},
    )


def _atlas_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads((_SCHEMA_DIR / "atlas.schema.json").read_text()))


def _warm(app: Flask) -> None:
    """Prime the (static) touch atlas cache, standing in for the background
    warmer, so a cache-only /frame/spec attaches them."""
    rd = Path(app.config["RENDERS_DIR"])
    for role in ("l20", "v28"):
        build_touch_atlas(role, renders_dir=rd, rasterize=_fake_rasterize)


def test_frame_spec_cold_cache_omits_atlases(app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    # A device poll must NEVER drive a browser render inline. On a cold cache the
    # spec ships without atlases (firmware draws chrome, per contract) and the
    # build is scheduled out-of-band. Stub the scheduler so no thread runs.
    monkeypatch.setattr(rest_api, "_schedule_touch_atlas_warm", lambda roles: None)
    client = app.test_client()
    token = _register(app, client)
    _seed(app, monkeypatch)
    doc = client.get("/api/v1/device/e1003/frame/spec", headers=_auth(token)).get_json()
    assert doc["primitives"]  # controls still present
    assert "atlases" not in doc  # but no atlases on a cold cache


def test_frame_spec_schedules_warm_on_cold_cache(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduled: list[list[str]] = []
    monkeypatch.setattr(
        rest_api, "_schedule_touch_atlas_warm", lambda roles: scheduled.append(roles)
    )
    client = app.test_client()
    token = _register(app, client)
    _seed(app, monkeypatch)
    client.get("/api/v1/device/e1003/frame/spec", headers=_auth(token))
    assert scheduled and sorted(scheduled[0]) == ["l20", "v28"]


def test_frame_spec_attaches_valid_atlases(app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    client = app.test_client()
    token = _register(app, client)
    _seed(app, monkeypatch)
    _warm(app)  # cache primed, as the background warmer would leave it
    doc = client.get("/api/v1/device/e1003/frame/spec", headers=_auth(token)).get_json()
    atlases = doc["atlases"]
    # switch label -> l20, slider value_text -> v28.
    assert sorted(a["id"] for a in atlases) == ["l20", "v28"]
    v = _atlas_validator()
    for a in atlases:
        assert not list(v.iter_errors(a))
        assert a["url"].endswith(f"/atlas/{a['digest']}")


def test_atlas_binary_is_served(app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    client = app.test_client()
    token = _register(app, client)
    _seed(app, monkeypatch)
    _warm(app)
    doc = client.get("/api/v1/device/e1003/frame/spec", headers=_auth(token)).get_json()
    digest = doc["atlases"][0]["digest"]
    resp = client.get(f"/api/v1/device/e1003/atlas/{digest}", headers=_auth(token))
    assert resp.status_code == 200
    assert len(resp.data) > 0
    assert "immutable" in resp.headers.get("Cache-Control", "")


def test_atlas_bad_digest_404(app: Flask) -> None:
    client = app.test_client()
    token = _register(app, client)
    assert client.get("/api/v1/device/e1003/atlas/..%2fx", headers=_auth(token)).status_code == 404
    assert client.get("/api/v1/device/e1003/atlas/zzzz", headers=_auth(token)).status_code == 404
