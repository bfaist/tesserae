"""Mono / 4-level grey boards: one kind per board whose firmware paints
either depth, chosen by frame length. The device advertises
``"gamuts": ["mono", "gray_4"]``; only then may it be rendered grey."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from flask import Flask
from PIL import Image

from app import device_loader, device_service, renderer_loader
from app.main import REPO_ROOT, create_app
from app.panel import device_panel

DUAL_DEPTH_KINDS = {
    "elecrow_crowpanel_579": 53856,
    "xiao_epaper_75": 96000,
    "xiao_epaper_panel_75_c3": 96000,
    "seeed_ee04_75": 96000,
    "waveshare_esp32_driver_75": 96000,
    "xteink_x4": 96000,
}
MONO_BYTES = {"elecrow_crowpanel_579": 26928}


@pytest.fixture
def registries(tmp_path: Path) -> Any:
    data_root = tmp_path / "devices"
    devices = device_loader.discover(
        REPO_ROOT / "devices",
        schema_path=REPO_ROOT / "schema" / "device.schema.json",
        data_root=data_root,
        hardware_dir=REPO_ROOT / "hardware",
        hardware_schema_path=REPO_ROOT / "schema" / "hardware.schema.json",
    )
    renderers = renderer_loader.discover(
        REPO_ROOT / "renderers",
        schema_path=REPO_ROOT / "schema" / "renderer.schema.json",
        data_root=tmp_path / "rdata",
    )
    assert devices.errors == []
    assert renderers.errors == []
    return devices, renderers, data_root


def _create(registries: Any, instance_id: str, kind_id: str) -> Any:
    devices, renderers, data_root = registries
    result = device_service.create_instance(
        devices=devices,
        renderers=renderers,
        data_root=data_root,
        instance_id=instance_id,
        kind_id=kind_id,
        transport="rest",
    )
    assert result.ok, result.error
    return result.device


def _pick(registries: Any, instance_id: str, renderer_id: str | None) -> Any:
    devices, renderers, data_root = registries
    result, _ = device_service.set_instance_renderer_id(
        devices=devices,
        renderers=renderers,
        data_root=data_root,
        instance_id=instance_id,
        renderer_id=renderer_id,
    )
    assert result.ok, result.error
    return result.device


def _advertise(registries: Any, instance_id: str, gamuts: list[str] | None) -> tuple[Any, bool]:
    devices, renderers, data_root = registries
    result, changed = device_service.update_advertised_gamuts(
        devices=devices,
        renderers=renderers,
        data_root=data_root,
        instance_id=instance_id,
        gamuts=gamuts,
    )
    assert result.ok, result.error
    return result.device, changed


def _composition(w: int, h: int) -> bytes:
    ramp = (np.arange(w) * 255 // max(1, w - 1)).astype(np.uint8)
    img = Image.fromarray(np.tile(ramp, (h, 1)), mode="L").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _render(registries: Any, device: Any) -> bytes:
    _devices, renderers, _ = registries
    (clone_id,) = device.renderer_ids
    panel = device_panel(device)
    return renderers.get(clone_id).transform(
        _composition(panel.w, panel.h), panel=panel, settings={"dither": "none"}
    )


# --- advertised gamuts -------------------------------------------------------


def test_parse_advertised_gamuts() -> None:
    parse = device_service.parse_advertised_gamuts
    assert parse({"gamuts": ["mono", "gray_4"]}) == ["mono", "gray_4"]
    assert parse(b'{"gamuts": ["gray_4", "gray_4", "plasma", 3]}') == ["gray_4"]
    assert parse({"gamuts": ["spectra_6"]}) == ["waveshare_e6"]
    assert parse({"gamuts": []}) == []
    # Absent or malformed: no information, keep the last known value.
    assert parse({"battery_pct": 80}) is None
    assert parse({"gamuts": "gray_4"}) is None
    assert parse(b"not json") is None


# --- catalog -----------------------------------------------------------------


@pytest.mark.parametrize("kind_id", sorted(DUAL_DEPTH_KINDS))
def test_dual_depth_kinds_offer_grey_behind_a_mono_default(registries: Any, kind_id: str) -> None:
    devices, _renderers, _ = registries
    kind = devices.get(kind_id)
    assert kind.renderer_ids == ["esp32_bw_bin", "esp32_gray2_bin"]
    assert kind.manifest["colour_depths"] == {"mono": "esp32_bw_bin", "gray_4": "esp32_gray2_bin"}
    assert kind.manifest["colour_depth_default"] == "mono"
    assert kind.panel["gamut"] == "mono"
    assert "UNVERIFIED ON HARDWARE" in kind.manifest["notes_md"]


def test_existing_grey_kinds_are_untouched(registries: Any) -> None:
    devices, _renderers, _ = registries
    for kind_id in ("seeed_reterminal_e1001_gray", "xteink_x4_gray"):
        kind = devices.get(kind_id)
        assert kind.renderer_ids == ["esp32_gray2_bin"]
        assert "colour_depths" not in kind.manifest


# --- the render gate ---------------------------------------------------------


@pytest.mark.parametrize("kind_id", sorted(DUAL_DEPTH_KINDS))
def test_new_device_renders_mono(registries: Any, kind_id: str) -> None:
    dev = _create(registries, "panel", kind_id)
    assert dev.renderer_ids == ["esp32_bw_bin__panel"]
    assert dev.panel["gamut"] == "mono"
    panel = device_panel(dev)
    expected = MONO_BYTES.get(kind_id, 48000)
    assert len(_render(registries, dev)) == expected == panel.native_w * panel.native_h // 8


def test_pinned_grey_without_capability_renders_mono(registries: Any) -> None:
    _create(registries, "xiao", "xiao_epaper_75")
    dev = _pick(registries, "xiao", "esp32_gray2_bin")
    # The pick is stored, but the firmware never said it paints grey.
    _, _, data_root = registries
    assert json.loads((data_root / "xiao.json").read_text())["renderer_id"] == "esp32_gray2_bin"
    assert dev.manifest["renderer_pick"] == "esp32_gray2_bin"
    assert dev.renderer_ids == ["esp32_bw_bin__xiao"]
    assert dev.panel["gamut"] == "mono"
    assert len(_render(registries, dev)) == 48000


def test_mono_only_advertisement_still_renders_mono(registries: Any) -> None:
    _create(registries, "xiao", "xiao_epaper_75")
    _pick(registries, "xiao", "esp32_gray2_bin")
    dev, changed = _advertise(registries, "xiao", ["mono"])
    assert changed is False
    assert dev.renderer_ids == ["esp32_bw_bin__xiao"]


@pytest.mark.parametrize("kind_id", sorted(DUAL_DEPTH_KINDS))
def test_pinned_grey_with_capability_renders_grey(registries: Any, kind_id: str) -> None:
    _create(registries, "panel", kind_id)
    _pick(registries, "panel", "esp32_gray2_bin")
    dev, changed = _advertise(registries, "panel", ["mono", "gray_4"])
    assert changed is True
    assert dev.renderer_ids == ["esp32_gray2_bin__panel"]
    assert dev.panel["gamut"] == "gray_4"
    assert device_panel(dev).gamut == "gray_4"
    assert len(_render(registries, dev)) == DUAL_DEPTH_KINDS[kind_id]


def test_crowpanel_grey_frame_is_198_bytes_per_row(registries: Any) -> None:
    _create(registries, "crow", "elecrow_crowpanel_579")
    _pick(registries, "crow", "esp32_gray2_bin")
    dev, _ = _advertise(registries, "crow", ["mono", "gray_4"])
    _devices, renderers, _ = registries
    img = Image.new("RGB", (792, 272), "white")
    img.paste((0, 0, 0), (0, 0, 4, 272))  # first four columns black
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    out = renderers.get("esp32_gray2_bin__crow").transform(
        buf.getvalue(), panel=device_panel(dev), settings={"dither": "none"}
    )
    assert len(out) == 792 * 272 // 4 == 53856
    # Each 198-byte row starts with the black byte, then white.
    for row in range(272):
        assert out[row * 198] == 0x00
        assert out[row * 198 + 1] == 0xFF


def test_x4_grey_frame_matches_the_x4_gray_kind_byte_for_byte(registries: Any) -> None:
    _create(registries, "x4", "xteink_x4")
    _pick(registries, "x4", "esp32_gray2_bin")
    dual, _ = _advertise(registries, "x4", ["mono", "gray_4"])
    legacy = _create(registries, "x4g", "xteink_x4_gray")
    assert device_panel(dual).model_dump() == device_panel(legacy).model_dump() | {
        "gamut": "gray_4"
    }
    a = _render(registries, dual)
    b = _render(registries, legacy)
    assert len(a) == 96000
    assert a == b


def test_firmware_rollback_falls_back_to_mono(registries: Any) -> None:
    _create(registries, "ee04", "seeed_ee04_75")
    _pick(registries, "ee04", "esp32_gray2_bin")
    _advertise(registries, "ee04", ["mono", "gray_4"])
    dev, changed = _advertise(registries, "ee04", ["mono"])
    assert changed is True
    assert dev.renderer_ids == ["esp32_bw_bin__ee04"]
    assert dev.panel["gamut"] == "mono"
    # The pick survives for when grey comes back.
    assert dev.manifest["renderer_pick"] == "esp32_gray2_bin"


def test_absent_gamuts_key_keeps_the_last_known_value(registries: Any) -> None:
    _create(registries, "wv", "waveshare_esp32_driver_75")
    _advertise(registries, "wv", ["mono", "gray_4"])
    dev, changed = _advertise(registries, "wv", None)
    assert changed is False
    assert dev.manifest["advertised_gamuts"] == ["mono", "gray_4"]


def test_capability_alone_does_not_switch_a_mono_device(registries: Any) -> None:
    """Default stays mono: advertising grey changes nothing until picked."""
    _create(registries, "c3", "xiao_epaper_panel_75_c3")
    dev, changed = _advertise(registries, "c3", ["mono", "gray_4"])
    assert changed is False
    assert dev.renderer_ids == ["esp32_bw_bin__c3"]


def test_explicit_mono_pick_is_kept_as_a_pin(registries: Any) -> None:
    _create(registries, "c3", "xiao_epaper_panel_75_c3")
    _advertise(registries, "c3", ["mono", "gray_4"])
    _pick(registries, "c3", "esp32_gray2_bin")
    dev = _pick(registries, "c3", "esp32_bw_bin")
    _, _, data_root = registries
    assert json.loads((data_root / "c3.json").read_text())["renderer_id"] == "esp32_bw_bin"
    assert dev.renderer_ids == ["esp32_bw_bin__c3"]


def test_default_flip_applies_only_to_capable_devices(registries: Any) -> None:
    """``colour_depth_default`` is the one place that would move a kind's
    default to grey; it never reaches a device that hasn't advertised it."""
    kind_manifest = {
        "renderers": ["esp32_bw_bin", "esp32_gray2_bin"],
        "colour_depths": {"mono": "esp32_bw_bin", "gray_4": "esp32_gray2_bin"},
        "colour_depth_default": "gray_4",
    }
    pick = device_loader.effective_renderer_pick
    assert pick(kind_manifest, None, ["mono", "gray_4"]) == "esp32_gray2_bin"
    assert pick(kind_manifest, None, ["mono"]) is None
    assert pick(kind_manifest, None, None) is None
    # An explicit mono pick beats the flipped default.
    assert pick(kind_manifest, "esp32_bw_bin", ["mono", "gray_4"]) == "esp32_bw_bin"
    # And with the default left at mono, a capable device stays mono.
    assert pick({**kind_manifest, "colour_depth_default": "mono"}, None, ["gray_4"]) is None


def test_kinds_without_colour_depths_are_not_gated(registries: Any) -> None:
    _create(registries, "kobo", "trmnl_client")
    dev = _pick(registries, "kobo", "trmnl_png_gray16")
    assert dev.renderer_ids == ["trmnl_png_gray16__kobo"]


def test_declared_bin_format_keeps_a_grey_bin_renderer(registries: Any) -> None:
    """A re-registering board that says ``format: bin`` must stay on its
    grey ``.bin`` renderer instead of being moved to the first ``.bin``."""
    devices, renderers, data_root = registries
    _create(registries, "xiao", "xiao_epaper_75")
    _pick(registries, "xiao", "esp32_gray2_bin")
    _advertise(registries, "xiao", ["mono", "gray_4"])
    result, changed = device_service.update_instance_renderer(
        devices=devices,
        renderers=renderers,
        data_root=data_root,
        instance_id="xiao",
        wire_format="bin",
    )
    assert changed is False
    assert result.device.renderer_ids == ["esp32_gray2_bin__xiao"]


def test_relay_mono_report_still_resolves_to_the_bare_kind(registries: Any, tmp_path: Path) -> None:
    from app.relay_pairing import RelayPairingPoller
    from app.state.settings_store import SettingsStore

    devices, renderers, data_root = registries
    poller = RelayPairingPoller(
        devices=devices,
        renderers=renderers,
        data_root=data_root,
        settings=SettingsStore(tmp_path / "settings.json"),
        run_async=False,
    )
    reported = {"model": "esp32_bw_client", "gamut": "mono", "panel_w": 800, "panel_h": 480}
    assert poller._resolve_kind("", "esp32_bw_client", reported) == "esp32_bw_client"


# --- REST register / status and the device card ------------------------------


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


def _register(app: Flask, device_id: str, kind: str, **extra: Any) -> tuple[Any, str]:
    client = app.test_client()
    client.post("/setup", data={"password": "abcdefgh", "password_confirm": "abcdefgh"})
    code = app.config["PAIRING_STORE"].issue(note="test").code
    resp = client.post(
        "/api/v1/device/register",
        headers={"X-Pairing-Code": code},
        json={"device_id": device_id, "kind": kind, "panel_w": 800, "panel_h": 480, **extra},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return client, resp.get_json()["device_token"]


def _status(client: Any, device_id: str, token: str, body: dict[str, Any]) -> None:
    resp = client.post(
        f"/api/v1/device/{device_id}/status",
        headers={"Authorization": f"Bearer {token}"},
        json={"battery_pct": 80, **body},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)


def test_register_stores_advertised_gamuts(app: Flask) -> None:
    _register(app, "xiao_a1", "xiao_epaper_75", gamuts=["mono", "gray_4"])
    dev = app.config["DEVICE_REGISTRY"].get("xiao_a1")
    assert dev.manifest["advertised_gamuts"] == ["mono", "gray_4"]
    raw = json.loads(dev.path.read_text(encoding="utf-8"))
    assert raw["advertised_gamuts"] == ["mono", "gray_4"]


def test_status_beats_update_the_capability_and_repaint(app: Flask, monkeypatch) -> None:
    client, token = _register(app, "xiao_b2", "xiao_epaper_75")
    registry = app.config["DEVICE_REGISTRY"]
    assert "advertised_gamuts" not in registry.get("xiao_b2").manifest

    repaints: list[str] = []
    push_mgr = app.config["PUSH_MANAGER"]
    monkeypatch.setattr(
        push_mgr, "invalidate_and_repaint", lambda device_id, **kw: repaints.append(device_id)
    )
    # A grey pick on firmware that hasn't advertised: still mono.
    device_service.set_instance_renderer_id(
        devices=registry,
        renderers=app.config["RENDERER_REGISTRY"],
        data_root=app.config["DEVICE_DATA_ROOT"],
        instance_id="xiao_b2",
        renderer_id="esp32_gray2_bin",
    )
    assert registry.get("xiao_b2").renderer_ids == ["esp32_bw_bin__xiao_b2"]

    _status(client, "xiao_b2", token, {"gamuts": ["mono", "gray_4"]})
    assert registry.get("xiao_b2").renderer_ids == ["esp32_gray2_bin__xiao_b2"]
    assert repaints == ["xiao_b2"]

    # A repeat of the same advertisement changes nothing.
    _status(client, "xiao_b2", token, {"gamuts": ["mono", "gray_4"]})
    assert repaints == ["xiao_b2"]

    # Firmware that says mono only: back to mono, repaint again.
    _status(client, "xiao_b2", token, {"gamuts": ["mono"]})
    assert registry.get("xiao_b2").renderer_ids == ["esp32_bw_bin__xiao_b2"]
    assert repaints == ["xiao_b2", "xiao_b2"]


def test_status_without_the_key_after_grey_falls_back_to_mono(app: Flask, monkeypatch) -> None:
    """The rollback hole: dual-depth firmware sends ``gamuts`` on every
    beat, so a beat without it is firmware from before the key (an OTA
    rollback or reflash), which rejects a grey frame on length. It must get
    mono, with the cached frame repainted, and the grey pick kept so a
    capable firmware later resumes grey without asking."""
    client, token = _register(app, "xiao_r1", "xiao_epaper_75", gamuts=["mono", "gray_4"])
    registry = app.config["DEVICE_REGISTRY"]
    device_service.set_instance_renderer_id(
        devices=registry,
        renderers=app.config["RENDERER_REGISTRY"],
        data_root=app.config["DEVICE_DATA_ROOT"],
        instance_id="xiao_r1",
        renderer_id="esp32_gray2_bin",
    )
    assert registry.get("xiao_r1").renderer_ids == ["esp32_gray2_bin__xiao_r1"]

    repaints: list[str] = []
    monkeypatch.setattr(
        app.config["PUSH_MANAGER"],
        "invalidate_and_repaint",
        lambda device_id, **kw: repaints.append(device_id),
    )
    _status(client, "xiao_r1", token, {})
    dev = registry.get("xiao_r1")
    assert dev.manifest["advertised_gamuts"] == ["mono"]
    assert dev.renderer_ids == ["esp32_bw_bin__xiao_r1"]
    assert dev.panel["gamut"] == "mono"
    assert repaints == ["xiao_r1"]
    assert json.loads(dev.path.read_text(encoding="utf-8"))["renderer_id"] == "esp32_gray2_bin"

    # Further keyless beats are steady: no writes, no repaints.
    _status(client, "xiao_r1", token, {})
    assert repaints == ["xiao_r1"]

    # Capable firmware comes back: grey resumes from the kept pick.
    _status(client, "xiao_r1", token, {"gamuts": ["mono", "gray_4"]})
    assert registry.get("xiao_r1").renderer_ids == ["esp32_gray2_bin__xiao_r1"]
    assert repaints == ["xiao_r1", "xiao_r1"]


def test_reregister_without_the_key_falls_back_to_mono(app: Flask) -> None:
    _register(app, "xiao_r2", "xiao_epaper_75", gamuts=["mono", "gray_4"])
    registry = app.config["DEVICE_REGISTRY"]
    device_service.set_instance_renderer_id(
        devices=registry,
        renderers=app.config["RENDERER_REGISTRY"],
        data_root=app.config["DEVICE_DATA_ROOT"],
        instance_id="xiao_r2",
        renderer_id="esp32_gray2_bin",
    )
    # The same unit registers again on older firmware: no ``gamuts`` key.
    client = app.test_client()
    code = app.config["PAIRING_STORE"].issue(note="test").code
    resp = client.post(
        "/api/v1/device/register",
        headers={"X-Pairing-Code": code},
        json={"device_id": "xiao_r2", "kind": "xiao_epaper_75", "panel_w": 800, "panel_h": 480},
    )
    assert resp.status_code == 200
    assert resp.get_json()["reused_existing"] is True
    dev = registry.get("xiao_r2")
    assert dev.manifest["advertised_gamuts"] == ["mono"]
    assert dev.renderer_ids == ["esp32_bw_bin__xiao_r2"]


def test_keyless_beats_never_write_a_device_that_never_advertised(app: Flask) -> None:
    client, token = _register(app, "xiao_r3", "xiao_epaper_75")
    _status(client, "xiao_r3", token, {})
    dev = app.config["DEVICE_REGISTRY"].get("xiao_r3")
    assert "advertised_gamuts" not in dev.manifest
    assert "advertised_gamuts" not in json.loads(dev.path.read_text(encoding="utf-8"))


def test_discover_without_the_key_leaves_the_capability_alone(app: Flask) -> None:
    _register(app, "xiao_r4", "xiao_epaper_75", gamuts=["mono", "gray_4"], mac="AA:BB:CC:00:11:22")
    client = app.test_client()
    resp = client.post(
        "/api/v1/device/discover",
        json={
            "device_id": "xiao_r4",
            "kind": "xiao_epaper_75",
            "mac": "AA:BB:CC:00:11:22",
            "panel_w": 800,
            "panel_h": 480,
        },
    )
    assert resp.status_code < 500
    dev = app.config["DEVICE_REGISTRY"].get("xiao_r4")
    assert dev.manifest["advertised_gamuts"] == ["mono", "gray_4"]


def test_heartbeats_off_the_rest_status_path_never_touch_it(app: Flask) -> None:
    """MQTT and relay beats go through ``record_status_heartbeat`` and never
    carry the key; their absence must not read as "mono only"."""
    from app.transport_wiring import record_status_heartbeat

    _register(app, "xiao_r5", "xiao_epaper_75", gamuts=["mono", "gray_4"])
    registry = app.config["DEVICE_REGISTRY"]
    record_status_heartbeat(
        app=app,
        device=registry.get("xiao_r5"),
        payload=b'{"battery_pct": 70}',
        status_cache=app.config["DEVICE_STATUS"],
        event_log=app.config["EVENT_LOG"],
        event_target="relay://xiao_r5/status",
    )
    assert registry.get("xiao_r5").manifest["advertised_gamuts"] == ["mono", "gray_4"]


def test_device_card_hides_grey_until_advertised(app: Flask) -> None:
    client, _token = _register(app, "xiao_c3", "xiao_epaper_75")
    body = client.get("/settings/devices/xiao_c3").get_data(as_text=True)
    assert "Colour depth" in body
    assert "A firmware update adds 4-level grey" in body
    assert "data-grey-prompt" not in body
    assert "data-grey-badge" not in client.get("/settings/devices").get_data(as_text=True)


def test_device_card_prompts_capable_mono_devices(app: Flask) -> None:
    client, token = _register(app, "xiao_d4", "xiao_epaper_75")
    _status(client, "xiao_d4", token, {"gamuts": ["mono", "gray_4"]})
    body = client.get("/settings/devices/xiao_d4").get_data(as_text=True)
    assert "data-grey-prompt" in body
    assert "Switch to grey" in body and "Keep mono" in body
    assert "A firmware update adds 4-level grey" not in body
    assert "data-grey-badge" in client.get("/settings/devices").get_data(as_text=True)

    # Keep mono: the prompt never comes back for this device.
    resp = client.post("/settings/devices/xiao_d4/colour-depth", data={"choice": "mono"})
    assert resp.status_code == 302
    dev = app.config["DEVICE_REGISTRY"].get("xiao_d4")
    assert json.loads(dev.path.read_text(encoding="utf-8"))["grey_prompt_dismissed"] is True
    assert dev.renderer_ids == ["esp32_bw_bin__xiao_d4"]
    assert "data-grey-prompt" not in client.get("/settings/devices/xiao_d4").get_data(as_text=True)
    assert "data-grey-badge" not in client.get("/settings/devices").get_data(as_text=True)


def test_switch_to_grey_from_the_prompt(app: Flask) -> None:
    client, token = _register(app, "xiao_e5", "xiao_epaper_75")
    _status(client, "xiao_e5", token, {"gamuts": ["mono", "gray_4"]})
    resp = client.post("/settings/devices/xiao_e5/colour-depth", data={"choice": "grey"})
    assert resp.status_code == 302
    dev = app.config["DEVICE_REGISTRY"].get("xiao_e5")
    assert dev.renderer_ids == ["esp32_gray2_bin__xiao_e5"]
    assert dev.panel["gamut"] == "gray_4"
    assert "data-grey-prompt" not in client.get("/settings/devices/xiao_e5").get_data(as_text=True)


def test_switch_to_grey_is_refused_without_capability(app: Flask) -> None:
    client, _token = _register(app, "xiao_f6", "xiao_epaper_75")
    client.post("/settings/devices/xiao_f6/colour-depth", data={"choice": "grey"})
    dev = app.config["DEVICE_REGISTRY"].get("xiao_f6")
    assert dev.renderer_ids == ["esp32_bw_bin__xiao_f6"]
    assert "renderer_id" not in json.loads(dev.path.read_text(encoding="utf-8"))
