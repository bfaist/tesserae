"""Panel colour depth: the gray_8 gamut (Inkplate 10), the TRMNL grey PNG
renderers (2-bit, and 4-bit for the TRMNL X), the effective gamut following
a gamut-dedicated renderer pick, and TRMNL catalog kinds that can actually
poll and follow their firmware version."""

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
from app.png_gray import encode_gray_png, png_bit_depth, png_colour_type

# --- fixtures ----------------------------------------------------------------


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


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    a = create_app(
        testing=True,
        data_root=tmp_path,
        plugins_dir=REPO_ROOT / "plugins",
        renderers_dir=REPO_ROOT / "renderers",
        devices_dir=REPO_ROOT / "devices",
    )
    return a


def _create(registries: Any, instance_id: str, kind_id: str, **kw: Any) -> Any:
    devices, renderers, data_root = registries
    result = device_service.create_instance(
        devices=devices,
        renderers=renderers,
        data_root=data_root,
        instance_id=instance_id,
        kind_id=kind_id,
        **kw,
    )
    assert result.ok, result.error
    return result.device


def _pick(registries: Any, instance_id: str, renderer_id: str | None) -> Any:
    devices, renderers, data_root = registries
    result, _changed = device_service.set_instance_renderer_id(
        devices=devices,
        renderers=renderers,
        data_root=data_root,
        instance_id=instance_id,
        renderer_id=renderer_id,
    )
    assert result.ok, result.error
    return result.device


# --- gray_8 gamut ------------------------------------------------------------


def test_gray_8_is_an_accepted_gamut_with_an_8_level_ramp() -> None:
    from app import quantizer as q

    assert "gray_8" in q.ACCEPTED_GAMUTS
    assert q.canonicalise_gamut("gray_8") == "gray_8"
    assert q.palette_for_gamut("gray_8") == q.GRAY_8_PALETTE
    assert [p[0] for p in q.GRAY_8_PALETTE] == [round(k * 255 / 7) for k in range(8)]
    assert q.GRAY_8_PALETTE[0] == (0, 0, 0) and q.GRAY_8_PALETTE[-1] == (255, 255, 255)


def test_reported_gray_16_is_refined_by_a_gray_8_kind_only() -> None:
    from app.quantizer import gamut_matches_report, refine_reported_gamut

    assert refine_reported_gamut("gray_8", "gray_16") == "gray_8"
    assert refine_reported_gamut("gray_16", "gray_16") == "gray_16"
    # Never the other way: a gray_16 kind is not narrowed by a gray_8 report,
    # and an unrelated kind keeps the report as declared.
    assert refine_reported_gamut("gray_16", "gray_8") == "gray_8"
    assert refine_reported_gamut("mono", "gray_16") == "gray_16"
    assert refine_reported_gamut(None, "spectra_6") == "waveshare_e6"
    assert gamut_matches_report("gray_8", "gray_16")
    assert gamut_matches_report("gray_8", "gray_8")
    assert not gamut_matches_report("gray_16", "gray_8")


def test_mcp_gamut_info_describes_gray_8() -> None:
    from app.mcp_api import _gamut_info

    info = _gamut_info("gray_8")
    assert info["color_mode"] == "8-level grayscale"
    assert len(info["colors"]) == 8
    assert info["mono"] is True


def test_gray_8_calibration_family_and_test_pattern(registries: Any) -> None:
    from app.palette_profiles.bundled import default_slug_for, list_bundled
    from app.test_patterns import _palette_for, gray_ramp_palette

    assert default_slug_for("gray_8") == "nominal-gray8"
    assert next(p.slug for p in list_bundled("gray_8")) == "nominal-gray8"
    palette, labels = _palette_for("gray_8", False)
    assert len(palette) == len(labels) == 8
    assert palette[-1] == (255, 255, 255)
    # The rounding change leaves the 4- and 16-level ramps exactly as before.
    assert gray_ramp_palette(4) == ((0, 0, 0), (85, 85, 85), (170, 170, 170), (255, 255, 255))
    assert gray_ramp_palette(16) == tuple((i * 17,) * 3 for i in range(16))


def test_inkplate_10_kind_is_gray_8(registries: Any) -> None:
    devices, _renderers, _ = registries
    kind = devices.get("soldered_inkplate_10")
    assert kind is not None
    assert kind.panel["gamut"] == "gray_8"
    assert kind.renderer_ids == ["esp32_gray_bin"]


def test_inkplate_reporting_gray_16_at_pairing_keeps_the_kinds_gray_8(registries: Any) -> None:
    """The partridgeworks firmware declares gray_16 on register / discover.
    The device must still land on the Inkplate kind and pack 8 levels."""
    dev = _create(
        registries,
        "inkplate",
        "soldered_inkplate_10",
        panel_overrides={"w": 1200, "h": 825, "gamut": "gray_16"},
        transport="rest",
    )
    assert dev.kind_of == "soldered_inkplate_10"
    assert dev.panel["gamut"] == "gray_8"
    assert device_panel(dev).gamut == "gray_8"


def test_other_kinds_keep_a_reported_gamut(registries: Any) -> None:
    dev = _create(registries, "reader", "koreader_client", panel_overrides={"gamut": "gray_4"})
    assert dev.panel["gamut"] == "gray_4"


def test_existing_gray_16_inkplate_instances_move_to_gray_8(registries: Any) -> None:
    devices, _renderers, data_root = registries
    _create(registries, "inkplate", "soldered_inkplate_10", transport="rest")
    # Simulate an instance created before the kind changed.
    path = data_root / "inkplate.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["panel"]["gamut"] = "gray_16"
    path.write_text(json.dumps(raw), encoding="utf-8")
    devices.devices.pop("inkplate")
    device_loader.load_instance_file(devices, inst_file=path, data_root=data_root)

    assert device_service.refine_inherited_gamuts(devices) == ["inkplate"]
    assert json.loads(path.read_text(encoding="utf-8"))["panel"]["gamut"] == "gray_8"
    assert devices.get("inkplate").panel["gamut"] == "gray_8"
    # Idempotent, and nothing else moves.
    assert device_service.refine_inherited_gamuts(devices) == []


def test_relay_gray_16_report_still_resolves_to_the_inkplate(
    registries: Any, tmp_path: Path
) -> None:
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
    reported = {"model": "esp32_bw_client", "gamut": "gray_16", "panel_w": 1200, "panel_h": 825}
    assert poller._resolve_kind("", "esp32_bw_client", reported) == "soldered_inkplate_10"


def test_rest_register_lands_inkplate_on_its_kind_as_gray_8(app: Flask) -> None:
    """End to end over the v1 REST register the Inkplate firmware uses: it
    names its catalog kind and says gray_16. Accepted, on the kind, gray_8."""
    code = app.config["PAIRING_STORE"].issue(note="test").code
    client = app.test_client()
    resp = client.post(
        "/api/v1/device/register",
        json={
            "device_id": "inkplate10_a1b2c3",
            "kind": "soldered_inkplate_10",
            "gamut": "gray_16",
            "panel_w": 1200,
            "panel_h": 825,
            "mac": "AA:BB:CC:DD:EE:01",
        },
        headers={"X-Pairing-Code": code},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    dev = app.config["DEVICE_REGISTRY"].get("inkplate10_a1b2c3")
    assert dev.kind_of == "soldered_inkplate_10"
    assert dev.panel["gamut"] == "gray_8"


# --- PNG encoder -------------------------------------------------------------


@pytest.mark.parametrize("depth", [1, 2, 4, 8])
def test_encode_gray_png_round_trips_through_pillow(depth: int) -> None:
    top = (1 << depth) - 1
    width = 13  # deliberately not a multiple of any samples-per-byte
    row = np.array([i % (top + 1) for i in range(width)], dtype=np.uint8)
    indices = np.tile(row, (3, 1))
    png = encode_gray_png(indices, bit_depth=depth)
    assert png_bit_depth(png) == depth
    assert png_colour_type(png) == 0
    img = Image.open(io.BytesIO(png))
    img.load()
    assert img.size == (width, 3)
    decoded = np.asarray(img.convert("L"), dtype=np.int32)
    expected = np.round(indices.astype(np.int32) * 255 / top).astype(np.int32)
    assert np.array_equal(decoded, expected)


def test_encode_gray_png_rejects_unsupported_depth() -> None:
    with pytest.raises(ValueError):
        encode_gray_png(np.zeros((1, 1), dtype=np.uint8), bit_depth=3)


# --- TRMNL kinds and renderers ----------------------------------------------


def test_trmnl_client_offers_the_2bit_renderer(registries: Any) -> None:
    devices, _renderers, _ = registries
    assert devices.get("trmnl_client").renderer_ids == [
        "trmnl_png",
        "trmnl_png_gray16",
        "trmnl_png_gray4",
    ]


def test_trmnl_x_kind_is_gray_16_on_the_16_grey_renderer(registries: Any) -> None:
    devices, _renderers, _ = registries
    kind = devices.get("trmnl_x")
    assert kind.panel["gamut"] == "gray_16"
    assert kind.renderer_ids == ["trmnl_png_gray16", "trmnl_png"]


def _composition(w: int, h: int) -> bytes:
    ramp = (np.arange(w) * 255 // max(1, w - 1)).astype(np.uint8)
    img = Image.fromarray(np.tile(ramp, (h, 1)), mode="L").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_trmnl_x_instance_renders_a_4bit_greyscale_png(registries: Any) -> None:
    _devices, renderers, _ = registries
    dev = _create(registries, "trmnl_x1", "trmnl_x", mac="A1:B2:C3:D4:E5:F6")
    assert dev.renderer_ids == ["trmnl_png_gray16__trmnl_x1"]
    clone = renderers.get("trmnl_png_gray16__trmnl_x1")
    assert clone.manifest["render_overrides"] == {"_png_bit_depth": 4}
    panel = device_panel(dev)
    assert panel.gamut == "gray_16"
    out = clone.transform(_composition(1872, 1404), panel=panel, settings={})
    assert png_bit_depth(out) == 4
    assert png_colour_type(out) == 0


def test_koreader_on_trmnl_client_keeps_the_8bit_grey_png(registries: Any) -> None:
    _devices, renderers, _ = registries
    _create(registries, "kobo", "trmnl_client")
    dev = _pick(registries, "kobo", "trmnl_png_gray16")
    clone = renderers.get("trmnl_png_gray16__kobo")
    assert "render_overrides" not in clone.manifest
    out = clone.transform(_composition(200, 100), panel=device_panel(dev), settings={})
    assert png_bit_depth(out) == 8


def test_trmnl_catalog_instances_mint_a_token_and_friendly_id(registries: Any) -> None:
    dev = _create(registries, "trmnl_x2", "trmnl_x")
    assert isinstance(dev.manifest.get("access_token"), str) and dev.manifest["access_token"]
    assert isinstance(dev.manifest.get("friendly_id"), str)


# --- gamut follows a gamut-dedicated renderer -------------------------------


def test_trmnl_x_1bit_pick_reports_mono_and_unpin_reverts(registries: Any) -> None:
    from app.mcp_api import _gamut_info

    _create(registries, "trmnl_x3", "trmnl_x")
    dev = _pick(registries, "trmnl_x3", "trmnl_png")
    assert dev.renderer_ids == ["trmnl_png__trmnl_x3"]
    assert dev.panel["gamut"] == "mono"
    assert device_panel(dev).gamut == "mono"
    assert _gamut_info(dev.panel["gamut"])["color_mode"] == "black & white"
    # The stored panel block keeps the kind's gamut; only the effective one moved.
    _devices, _renderers, data_root = registries
    raw = json.loads((data_root / "trmnl_x3.json").read_text(encoding="utf-8"))
    assert raw["panel"]["gamut"] == "gray_16"

    dev = _pick(registries, "trmnl_x3", None)
    assert dev.renderer_ids == ["trmnl_png_gray16__trmnl_x3"]
    assert dev.panel["gamut"] == "gray_16"


def test_trmnl_client_grey_picks_move_the_gamut(registries: Any) -> None:
    dev = _create(registries, "og", "trmnl_client")
    # The 1-bit default reports mono rather than the colour fallback.
    assert dev.panel["gamut"] == "mono"
    dev = _pick(registries, "og", "trmnl_png_gray16")
    assert dev.panel["gamut"] == "gray_16"
    dev = _pick(registries, "og", "trmnl_png_gray4")
    assert dev.panel["gamut"] == "gray_4"
    dev = _pick(registries, "og", "trmnl_png")
    assert dev.panel["gamut"] == "mono"


def test_ordinary_renderer_pick_leaves_the_gamut_alone(registries: Any) -> None:
    """Renderers that declare no gamuts follow the panel block, as before."""
    dev = _create(registries, "reader2", "koreader_client")
    assert dev.panel["gamut"] == "gray_16"
    dev = _pick(registries, "reader2", "esp32_bw_bin")
    assert dev.panel["gamut"] == "gray_16"


def test_gamut_override_survives_a_panel_block_patch(registries: Any) -> None:
    """``app.trmnl_api`` patches ``manifest["panel"]`` in place when it adopts
    a reported buffer; the renderer-led gamut must not fall off with it."""
    dev = _create(registries, "og2", "trmnl_client")
    dev = _pick(registries, "og2", "trmnl_png_gray4")
    dev.manifest["panel"] = {"w": 800, "h": 480, "native_w": 800, "native_h": 480}
    assert dev.panel["gamut"] == "gray_4"


# --- firmware-led renderer on the XIAO 7.5" TRMNL kind -----------------------


def _sync(registries: Any, instance_id: str, fw: str) -> tuple[Any, bool]:
    devices, renderers, data_root = registries
    result, changed = device_service.sync_renderer_for_firmware(
        devices=devices,
        renderers=renderers,
        data_root=data_root,
        instance_id=instance_id,
        fw_version=fw,
    )
    assert result.ok
    return result.device, changed


def test_parse_firmware_version() -> None:
    parse = device_service.parse_firmware_version
    assert parse("1.6.0") == (1, 6, 0)
    assert parse("v1.6.1-rc2") == (1, 6, 1)
    assert parse("1.10") == (1, 10)
    assert parse("KOReader/2024.11") is None
    assert parse("") is None
    assert parse(None) is None


def test_xiao_trmnl_follows_its_firmware_version(registries: Any) -> None:
    _create(registries, "xiao", "seeed_xiao_75")
    dev, changed = _sync(registries, "xiao", "1.5.9")
    assert changed is False
    assert dev.renderer_ids == ["trmnl_png__xiao"]

    dev, changed = _sync(registries, "xiao", "1.6.0")
    assert changed is True
    assert dev.renderer_ids == ["trmnl_png_gray4__xiao"]
    assert dev.panel["gamut"] == "gray_4"
    _, _, data_root = registries
    raw = json.loads((data_root / "xiao.json").read_text(encoding="utf-8"))
    assert raw["renderer_id"] == "trmnl_png_gray4" and raw["renderer_auto"] is True

    dev, changed = _sync(registries, "xiao", "1.7")
    assert changed is False

    # A downgrade goes back to 1-bit and drops the automatic pin.
    dev, changed = _sync(registries, "xiao", "1.5.2")
    assert changed is True
    assert dev.renderer_ids == ["trmnl_png__xiao"]
    assert dev.panel["gamut"] == "mono"
    raw = json.loads((data_root / "xiao.json").read_text(encoding="utf-8"))
    assert "renderer_id" not in raw and "renderer_auto" not in raw

    # An unreadable version changes nothing.
    _, changed = _sync(registries, "xiao", "garbage")
    assert changed is False


def test_operator_pick_beats_the_firmware_rule(registries: Any) -> None:
    _create(registries, "xiao2", "seeed_xiao_75")
    _sync(registries, "xiao2", "1.6.0")
    # Operator goes back to 1-bit on a 1.6 firmware: that sticks.
    dev = _pick(registries, "xiao2", "trmnl_png")
    assert dev.renderer_ids == ["trmnl_png__xiao2"]
    dev, changed = _sync(registries, "xiao2", "1.6.0")
    assert changed is False
    assert dev.renderer_ids == ["trmnl_png__xiao2"]


def test_kinds_without_firmware_rules_never_move(registries: Any) -> None:
    _create(registries, "og3", "trmnl_client")
    dev, changed = _sync(registries, "og3", "9.9.9")
    assert changed is False
    assert dev.renderer_ids == ["trmnl_png__og3"]


# --- TRMNL BYOS endpoints ----------------------------------------------------


def test_trmnl_x_auto_provisions_on_its_catalog_kind(app: Flask) -> None:
    client = app.test_client()
    resp = client.get(
        "/api/setup",
        headers={"Id": "A1:B2:C3:D4:E5:F7", "Model": "x", "FW-Version": "1.6.0"},
    )
    assert resp.status_code == 200
    devs = app.config["DEVICE_REGISTRY"]
    (device,) = [d for d in devs.all() if d.manifest.get("mac") == "A1:B2:C3:D4:E5:F7"]
    assert device.kind_of == "trmnl_x"
    assert device.panel["w"] == 1872 and device.panel["h"] == 1404
    assert device.panel["gamut"] == "gray_16"
    assert device.renderer_ids == [f"trmnl_png_gray16__{device.id}"]

    # The catalog instance answers /api/display by MAC and by token.
    resp = client.get("/api/display", headers={"Id": "A1:B2:C3:D4:E5:F7"})
    assert resp.status_code == 200
    token = device.manifest["access_token"]
    resp = client.get("/api/display", headers={"access-token": token})
    assert resp.status_code == 200


def test_unknown_model_still_provisions_the_bare_kind(app: Flask) -> None:
    client = app.test_client()
    client.get("/api/setup", headers={"Id": "A1:B2:C3:D4:E5:F8", "Model": "og"})
    devs = app.config["DEVICE_REGISTRY"]
    (device,) = [d for d in devs.all() if d.manifest.get("mac") == "A1:B2:C3:D4:E5:F8"]
    assert device.kind_of == "trmnl_client"


def test_display_poll_moves_a_xiao_onto_the_2bit_renderer(app: Flask) -> None:
    registry = app.config["DEVICE_REGISTRY"]
    result = device_service.create_instance(
        devices=registry,
        renderers=app.config["RENDERER_REGISTRY"],
        data_root=app.config["DEVICE_DATA_ROOT"],
        instance_id="xiao_trmnl",
        kind_id="seeed_xiao_75",
        mac="11:22:33:44:55:77",
    )
    assert result.ok
    client = app.test_client()
    resp = client.get(
        "/api/display",
        headers={"Id": "11:22:33:44:55:77", "FW-Version": "1.6.2", "Model": "seeed_esp32c3"},
    )
    assert resp.status_code == 200
    assert registry.get("xiao_trmnl").renderer_ids == ["trmnl_png_gray4__xiao_trmnl"]

    # KOReader's User-Agent is never read as a firmware version.
    resp = client.get(
        "/api/display",
        headers={"Id": "11:22:33:44:55:77", "User-Agent": "1.0.0"},
    )
    assert resp.status_code == 200
    assert registry.get("xiao_trmnl").renderer_ids == ["trmnl_png_gray4__xiao_trmnl"]
