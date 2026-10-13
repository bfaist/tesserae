"""Bloomin8 Canvas Schedule Pull API (#344).

The Canvas's HTTP client is strict and nobody here has one to test on, so
these lock down the wire contract from Bloomin8's Schedule_Pull_API.md:
every control reply is HTTP 200 with the outcome in the JSON ``status``
and a future UTC ``next_cron_time``; the frame is a baseline JPEG stored
portrait with an exact Content-Length; a painted frame isn't served again.
"""

from __future__ import annotations

import io
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from flask import Flask
from PIL import Image

from app import device_service
from app.main import REPO_ROOT, create_app

TOKEN = "Abcdefghij0123456789"
DIGEST = "0123456789abcdef"


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    app = create_app(
        testing=True,
        data_root=tmp_path,
        plugins_dir=REPO_ROOT / "plugins",
        renderers_dir=REPO_ROOT / "renderers",
    )
    result = device_service.create_instance(
        devices=app.config["DEVICE_REGISTRY"],
        renderers=app.config["RENDERER_REGISTRY"],
        data_root=app.config["DEVICE_DATA_ROOT"],
        instance_id="canvas",
        kind_id="bloomin8_canvas",
        name="Hall canvas",
        access_token=TOKEN,
    )
    assert result.ok, result.error
    return app


def _render(app: Flask, w: int, h: int, digest: str = DIGEST) -> None:
    """Put a composition PNG on disk and make it the device's latest."""
    renders = Path(app.config["RENDERS_DIR"])
    renders.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), (200, 30, 30)).save(renders / f"{digest}.png")
    app.config["PUSH_MANAGER"]._latest_renders["canvas"] = {
        "digest": digest,
        "composition_digest": digest,
        "ext": "png",
        "filename": f"{digest}.png",
    }


def _pull(client, token: str = TOKEN, pull_id: str = "p1"):
    return client.get(
        f"/api/bloomin8/eink_pull?device_id=B8-01&pull_id={pull_id}"
        "&cron_time=2026-10-13T08:00:00Z&battery=81",
        headers={"X-Access-Token": token},
    )


def _future(body: dict) -> float:
    raw = body["data"]["next_cron_time"]
    assert raw.endswith("Z")
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


def test_new_instance_gets_long_token_and_no_friendly_id(app: Flask) -> None:
    result = device_service.create_instance(
        devices=app.config["DEVICE_REGISTRY"],
        renderers=app.config["RENDERER_REGISTRY"],
        data_root=app.config["DEVICE_DATA_ROOT"],
        instance_id="canvas2",
        kind_id="bloomin8_canvas",
    )
    assert result.ok and result.device is not None
    assert len(result.device.manifest["access_token"]) == 20
    assert "friendly_id" not in result.device.manifest


def test_unknown_token_is_200_with_a_retry_wake(app: Flask) -> None:
    resp = _pull(app.test_client(), token="nope")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == 401
    assert _future(body) > time.time() + 3000


def test_nothing_rendered_is_204_in_body(app: Flask) -> None:
    resp = _pull(app.test_client())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == 204
    assert "image_url" not in body["data"]
    assert _future(body) >= time.time() + 59


def test_pull_serves_portrait_frame_and_records_battery(app: Flask) -> None:
    _render(app, 1200, 1600)
    client = app.test_client()
    body = _pull(client).get_json()
    assert body["status"] == 200
    assert body["type"] == "SHOW"
    url = body["data"]["image_url"]
    assert url.endswith(f"/api/bloomin8/frame/canvas/{DIGEST}_P.jpg")
    assert _future(body) >= time.time() + 59

    status = app.config["DEVICE_STATUS"]["canvas"]["parsed"]
    assert status["battery_pct"] == 81
    assert status["hw_id"] == "B8-01"

    img = client.get(url.split("localhost", 1)[1])
    assert img.status_code == 200
    assert img.headers["Content-Type"] == "image/jpeg"
    assert int(img.headers["Content-Length"]) == len(img.data)
    assert "Transfer-Encoding" not in img.headers
    assert "Content-Encoding" not in img.headers
    with Image.open(io.BytesIO(img.data)) as jpeg:
        assert jpeg.format == "JPEG"
        assert jpeg.size == (1200, 1600)
        assert not jpeg.info.get("progressive")
        assert not jpeg.info.get("progression")


def test_landscape_composition_is_stored_portrait_as_L(app: Flask) -> None:
    _render(app, 1600, 1200)
    client = app.test_client()
    url = _pull(client).get_json()["data"]["image_url"]
    assert url.endswith("_L.jpg")
    img = client.get(url.split("localhost", 1)[1])
    with Image.open(io.BytesIO(img.data)) as jpeg:
        assert jpeg.size == (1200, 1600)


def test_painted_frame_is_not_served_again(app: Flask) -> None:
    _render(app, 1200, 1600)
    client = app.test_client()
    assert _pull(client, pull_id="p1").get_json()["status"] == 200
    sig = client.get(
        "/api/bloomin8/eink_signal?pull_id=p1&success=1",
        headers={"X-Access-Token": TOKEN},
    )
    assert sig.status_code == 200 and sig.get_json()["status"] == 200
    again = _pull(client, pull_id="p2").get_json()
    assert again["status"] == 204
    assert _future(again) >= time.time() + 59


def test_failed_paint_is_served_again(app: Flask) -> None:
    _render(app, 1200, 1600)
    client = app.test_client()
    _pull(client, pull_id="p1")
    client.get(
        "/api/bloomin8/eink_signal?pull_id=p1&success=0",
        headers={"X-Access-Token": TOKEN},
    )
    assert _pull(client, pull_id="p2").get_json()["status"] == 200


def test_frame_route_serves_only_handed_out_frames(app: Flask) -> None:
    _render(app, 1200, 1600)
    client = app.test_client()
    # Never pulled: the digest exists on disk but wasn't handed out.
    assert client.get(f"/api/bloomin8/frame/canvas/{DIGEST}_P.jpg").status_code == 404
    _pull(client)
    assert client.get(f"/api/bloomin8/frame/canvas/{DIGEST}_P.jpg").status_code == 200
    assert client.get("/api/bloomin8/frame/canvas/../x_P.jpg").status_code == 404


def test_parse_status_rejects_out_of_range_battery(app: Flask) -> None:
    kind = app.config["DEVICE_REGISTRY"].get("bloomin8_canvas")
    assert kind.parse_status(b'{"battery": "140"}')["battery_pct"] is None
    assert kind.parse_status(b'{"battery": "55"}')["battery_pct"] == 55
