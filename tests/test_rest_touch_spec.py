"""GET /frame/spec: resolves a device's current canvas page to the touch-v3
wire spec (device-owned touch delivery)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from flask import Flask

import app.rest_api as rest_api
from app.main import REPO_ROOT, create_app
from app.state.page_store import Page
from app.state.panel_store import CanvasLayout, Element


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


def _register(app: Flask, client, device_id: str) -> str:
    client.post("/setup", data={"password": "abcdefgh", "password_confirm": "abcdefgh"})
    code = app.config["PAIRING_STORE"].issue(note="test").code
    resp = client.post(
        "/api/v1/device/register",
        headers={"X-Pairing-Code": code, "Content-Type": "application/json"},
        data=json.dumps(
            {"device_id": device_id, "kind": "esp32_client", "panel_w": 1872, "panel_h": 1404}
        ),
    )
    assert resp.status_code == 201
    return resp.get_json()["device_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_frame(app: Flask, monkeypatch: pytest.MonkeyPatch, page_id: str) -> None:
    """Point the device's 'current frame' at page_id without a real render."""
    monkeypatch.setattr(
        app.config["PUSH_MANAGER"], "latest_render_for", lambda _did: {"digest": "a" * 16}
    )
    monkeypatch.setattr(
        rest_api,
        "_frame_info_for_digest",
        lambda _dev, _dig: {"page_id": page_id, "composition_digest": "c"},
    )


def test_frame_spec_returns_primitives(app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    client = app.test_client()
    token = _register(app, client, "e1003")
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
                        value_key="ha:light.desk",
                        state="on",
                    ),
                    Element(id="btn", kind="button", x=200, y=10, w=160, h=80, on_tap="refresh"),
                ]
            ),
        )
    )
    _seed_frame(app, monkeypatch, "p1")

    resp = client.get("/api/v1/device/e1003/frame/spec", headers=_auth(token))
    assert resp.status_code == 200
    doc = resp.get_json()
    assert [p["id"] for p in doc["primitives"]] == ["sw", "btn"]
    assert len(doc["layout_digest"]) == 16


def test_frame_spec_layout_param_is_advisory_not_blocking(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ?layout=<held digest> is advisory: the endpoint returns the current spec
    # immediately regardless, it never long-polls waiting for a change.
    # Passing the CURRENT layout digest (the "I already hold this" case) must
    # return the same body, not hang.
    client = app.test_client()
    token = _register(app, client, "e1003")
    app.config["PAGE_STORE"].save(
        Page(
            id="p1",
            name="Touch",
            layout_kind="canvas",
            canvas=CanvasLayout(
                els=[Element(id="btn", kind="button", x=10, y=10, w=160, h=80, on_tap="refresh")]
            ),
        )
    )
    _seed_frame(app, monkeypatch, "p1")

    plain = client.get("/api/v1/device/e1003/frame/spec", headers=_auth(token)).get_json()
    held = client.get(
        f"/api/v1/device/e1003/frame/spec?layout={plain['layout_digest']}",
        headers=_auth(token),
    )
    assert held.status_code == 200
    assert held.get_json()["layout_digest"] == plain["layout_digest"]
    assert held.get_json()["primitives"] == plain["primitives"]


def test_frame_spec_grid_page_is_empty(app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    client = app.test_client()
    token = _register(app, client, "e1003")
    app.config["PAGE_STORE"].save(Page(id="g1", name="Grid", layout_kind="grid"))
    _seed_frame(app, monkeypatch, "g1")

    resp = client.get("/api/v1/device/e1003/frame/spec", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.get_json()["primitives"] == []


def test_frame_spec_no_frame_404(app: Flask) -> None:
    client = app.test_client()
    token = _register(app, client, "e1003")
    # No render seeded, so the device has no current frame.
    resp = client.get("/api/v1/device/e1003/frame/spec", headers=_auth(token))
    assert resp.status_code == 404


def test_frame_spec_requires_auth(app: Flask) -> None:
    client = app.test_client()
    _register(app, client, "e1003")
    resp = client.get("/api/v1/device/e1003/frame/spec")
    assert resp.status_code in (401, 403)


def _button_page(app: Flask) -> None:
    app.config["PAGE_STORE"].save(
        Page(
            id="p1",
            name="Touch",
            layout_kind="canvas",
            canvas=CanvasLayout(
                els=[
                    Element(
                        id="btn",
                        kind="button",
                        x=10,
                        y=10,
                        w=160,
                        h=80,
                        on_tap="refresh",
                        label="Lights",
                    )
                ]
            ),
        )
    )


def _fake_atlas(monkeypatch: pytest.MonkeyPatch, digest: str) -> None:
    import app.touch_atlas as touch_atlas

    def fake(role_id: str, *, renders_dir: Path, rasterize: object) -> dict[str, object]:
        return {"id": role_id, "digest": digest, "format": "gray4", "glyphs": {}}

    monkeypatch.setattr(touch_atlas, "build_touch_atlas", fake)


def test_rebuilt_atlas_moves_the_layout_digest(app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    """#360: the firmware keeps its held spec and atlas strips while the layout
    digest is unchanged, so a rebuilt strip under the same controls has to move
    the digest or the panel keeps drawing the old one."""
    client = app.test_client()
    token = _register(app, client, "e1003")
    _button_page(app)
    _seed_frame(app, monkeypatch, "p1")

    _fake_atlas(monkeypatch, "1" * 16)
    first = client.get("/api/v1/device/e1003/frame/spec", headers=_auth(token)).get_json()
    again = client.get("/api/v1/device/e1003/frame/spec", headers=_auth(token)).get_json()
    assert first["atlases"] and first["layout_digest"] == again["layout_digest"]

    _fake_atlas(monkeypatch, "2" * 16)
    rebuilt = client.get("/api/v1/device/e1003/frame/spec", headers=_auth(token)).get_json()
    assert rebuilt["atlases"][0]["digest"] == "2" * 16
    assert rebuilt["layout_digest"] != first["layout_digest"]
    assert len(rebuilt["layout_digest"]) == 16


def test_atlases_arriving_later_move_the_layout_digest(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec served on a cold atlas cache carries no strips; the firmware only
    retries strips its held spec lists, so the warmed spec needs a new digest."""
    import app.touch_atlas as touch_atlas

    client = app.test_client()
    token = _register(app, client, "e1003")
    _button_page(app)
    _seed_frame(app, monkeypatch, "p1")
    monkeypatch.setattr(rest_api, "_schedule_touch_atlas_warm", lambda _roles: None)

    monkeypatch.setattr(touch_atlas, "build_touch_atlas", lambda *_a, **_k: None)
    cold = client.get("/api/v1/device/e1003/frame/spec", headers=_auth(token)).get_json()
    assert "atlases" not in cold

    _fake_atlas(monkeypatch, "1" * 16)
    warm = client.get("/api/v1/device/e1003/frame/spec", headers=_auth(token)).get_json()
    assert warm["atlases"]
    assert warm["layout_digest"] != cold["layout_digest"]
