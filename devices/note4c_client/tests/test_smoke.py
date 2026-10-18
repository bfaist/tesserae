"""NOTE4C registration, telemetry, sleep configuration and frame format."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from flask import Flask
from PIL import Image

from app.device_loader import discover
from app.main import REPO_ROOT
from app.quantizer import pack_to_panel_bin
from app.rest_api import _configured_poll_s


@pytest.fixture
def note(tmp_path):
    registry = discover(
        REPO_ROOT / "devices",
        schema_path=REPO_ROOT / "schema/device.schema.json",
        hardware_dir=REPO_ROOT / "hardware",
        hardware_schema_path=REPO_ROOT / "schema/hardware.schema.json",
        data_root=tmp_path,
    )
    assert registry.errors == []
    device = registry.get("note4c_client")
    assert device is not None
    return device


def test_manifest(note):
    assert note.name == "ZecTrix NOTE4C Devkit"
    assert note.renderer_ids == ["esp32_bin"]
    panel = note.manifest["panel"]
    assert (panel["w"], panel["h"], panel["gamut"], panel["vflip"]) == (400, 300, "bwry_4", False)
    interval = note.config_schema["sleep_interval_s"]
    assert (interval["default"], interval["min"], interval["max"]) == (900, 30, 604800)


@pytest.mark.parametrize("saved, expected", [(None, 900), (300, 300), (3600, 3600)])
def test_default_and_saved_sleep_interval(note, saved, expected):
    settings = {} if saved is None else {note.id: {"sleep_interval_s": saved}}
    app = Flask(__name__)
    app.config["SETTINGS_STORE"] = SimpleNamespace(get_section=lambda section: settings)
    with app.app_context():
        assert _configured_poll_s(note) == expected


@pytest.mark.parametrize("interval", [30, 900, 604800])
def test_valid_sleep_interval(note, interval):
    assert note.validate_config({"sleep_interval_s": interval}) == (True, None)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"sleep_interval_s": 29},
        {"sleep_interval_s": 604801},
        {"sleep_interval_s": "bad"},
        {"sleep_interval_s": None},
    ],
)
def test_invalid_sleep_interval(note, payload):
    valid, error = note.validate_config(payload)
    assert not valid
    assert error


def test_telemetry(note):
    parsed = note.parse_status(
        json.dumps(
            {
                "battery_mv": "4000",
                "battery_pct": 80,
                "rssi": "-60",
                "ip": "192.0.2.1",
                "sleep_until": "1700000900",
                "next_sleep_s": "900",
                "kind": "note4c_client",
                "fw_version": "1.1.1",
                "wake_reason": "timer",
            }
        ).encode()
    )
    assert parsed == {
        "battery_mv": 4000,
        "battery_pct": 80,
        "rssi": -60,
        "ip": "192.0.2.1",
        "sleep_until": 1700000900.0,
        "next_sleep_s": 900,
        "kind": "note4c_client",
        "fw_version": "1.1.1",
        "wake_reason": "timer",
    }


def test_empty_telemetry(note):
    assert note.parse_status(b"") == dict.fromkeys(
        ("battery_mv", "battery_pct", "rssi", "ip", "sleep_until", "next_sleep_s")
    )


@pytest.mark.parametrize("payload", [b"not-json", b"\xff", b"[]", b"null"])
def test_malformed_telemetry(note, payload):
    assert "error" in note.parse_status(payload)


def test_bwry_frame_layout(note):
    panel = note.manifest["panel"]
    image = Image.new("RGB", (panel["w"], panel["h"]), "white")
    for x, colour in enumerate([(0, 0, 0), (255, 255, 255), (255, 255, 0), (255, 0, 0)]):
        image.putpixel((x, 0), colour)
    image.putpixel((399, 299), (255, 0, 0))
    packed = pack_to_panel_bin(
        image,
        width=panel["w"],
        height=panel["h"],
        gamut=panel["gamut"],
        dither="none",
        calibrated=False,
    )
    assert len(packed) == 30000
    assert packed[0] == 0x1B
    assert packed[-1] == 0x57
    assert packed[1:-1] == bytes([0x55]) * 29998
