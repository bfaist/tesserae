"""Every bundled widget's options panel follows docs/widgets.md
"Keep the options panel short".

* About five options always visible (no ``show_if``, no ``advanced``),
  with an explicit allowlist for a plugin deliberately left above that.
* ``show_if`` only names options the widget has, in a shape the editor
  understands (a value, a list of values, or ``{"not": ...}``), and never
  points at itself.
* ``advanced`` is a plain ``true`` when present.
* No em dashes in any option label, help or choice label.
* Tidying a panel never breaks a saved cell: every option name, choice
  value and default in ``tests/fixtures/bundled_option_contract.json`` is
  still there unchanged. Add to the snapshot when a widget gains an
  option; change it only for a deliberate, announced break.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.main import REPO_ROOT

PLUGINS = REPO_ROOT / "plugins"
CONTRACT = Path(__file__).parent / "fixtures" / "bundled_option_contract.json"

# The most options a panel shows before anything is picked.
MAX_UP_FRONT = 5

# Plugins deliberately left with more than MAX_UP_FRONT always-visible
# options, and why. Empty today: every bundled widget fits.
UP_FRONT_ALLOWLIST: dict[str, str] = {}

EM_DASH = "—"


def _manifests() -> list[tuple[str, dict[str, Any]]]:
    out = []
    for plugin_dir in sorted(PLUGINS.iterdir()):
        path = plugin_dir / "plugin.json"
        if path.is_file():
            out.append((plugin_dir.name, json.loads(path.read_text(encoding="utf-8"))))
    return out


MANIFESTS = _manifests()
WITH_OPTIONS = [(pid, m) for pid, m in MANIFESTS if m.get("cell_options")]
IDS = [pid for pid, _ in WITH_OPTIONS]


def _up_front(options: list[dict[str, Any]]) -> list[str]:
    return [o["name"] for o in options if "show_if" not in o and not o.get("advanced")]


@pytest.mark.parametrize(("pid", "manifest"), WITH_OPTIONS, ids=IDS)
def test_about_five_options_up_front(pid: str, manifest: dict[str, Any]) -> None:
    up_front = _up_front(manifest["cell_options"])
    if pid in UP_FRONT_ALLOWLIST:
        return
    assert len(up_front) <= MAX_UP_FRONT, (
        f"{pid} shows {len(up_front)} options up front ({', '.join(up_front)}); "
        "nest the rest with show_if or mark fine-tuning advanced, or allowlist it with a reason"
    )


def test_allowlist_only_names_plugins_still_above_the_limit() -> None:
    by_id = dict(WITH_OPTIONS)
    for pid in UP_FRONT_ALLOWLIST:
        assert pid in by_id, f"allowlisted {pid} is not a bundled plugin with options"
        assert len(_up_front(by_id[pid]["cell_options"])) > MAX_UP_FRONT, (
            f"{pid} is within the limit now; drop it from the allowlist"
        )


def _valid_expected(expected: Any) -> bool:
    scalar = (str, int, float, bool)
    if isinstance(expected, dict):
        return set(expected) == {"not"} and _valid_expected(expected["not"])
    if isinstance(expected, list):
        return bool(expected) and all(isinstance(v, scalar) for v in expected)
    return isinstance(expected, scalar)


@pytest.mark.parametrize(("pid", "manifest"), WITH_OPTIONS, ids=IDS)
def test_show_if_references_are_valid(pid: str, manifest: dict[str, Any]) -> None:
    names = [o["name"] for o in manifest["cell_options"]]
    for opt in manifest["cell_options"]:
        cond = opt.get("show_if")
        if cond is None:
            continue
        assert isinstance(cond, dict) and cond, f"{pid}.{opt['name']}: show_if must be an object"
        for key, expected in cond.items():
            assert key in names, f"{pid}.{opt['name']}: show_if names unknown option {key!r}"
            assert key != opt["name"], f"{pid}.{opt['name']}: show_if names itself"
            assert _valid_expected(expected), f"{pid}.{opt['name']}: show_if {key!r} shape"


@pytest.mark.parametrize(("pid", "manifest"), WITH_OPTIONS, ids=IDS)
def test_show_if_controller_comes_first(pid: str, manifest: dict[str, Any]) -> None:
    """An option follows the one it depends on, so the panel reads in order."""
    position = {o["name"]: i for i, o in enumerate(manifest["cell_options"])}
    for opt in manifest["cell_options"]:
        for key in opt.get("show_if") or {}:
            assert position[key] < position[opt["name"]], (
                f"{pid}.{opt['name']} is listed before {key}, which it depends on"
            )


@pytest.mark.parametrize(("pid", "manifest"), WITH_OPTIONS, ids=IDS)
def test_advanced_options_are_valid(pid: str, manifest: dict[str, Any]) -> None:
    options = manifest["cell_options"]
    for opt in options:
        if "advanced" in opt:
            assert opt["advanced"] is True, f"{pid}.{opt['name']}: advanced must be true"
    flags = [bool(o.get("advanced")) for o in options]
    if any(flags):
        first = flags.index(True)
        assert all(flags[first:]), f"{pid}: advanced options belong at the end of the list"
        assert not all(flags), f"{pid}: every option is advanced; keep the essentials up front"


@pytest.mark.parametrize(("pid", "manifest"), WITH_OPTIONS, ids=IDS)
def test_no_em_dashes_in_option_text(pid: str, manifest: dict[str, Any]) -> None:
    for opt in manifest["cell_options"]:
        assert EM_DASH not in opt.get("label", ""), f"{pid}.{opt['name']} label"
        assert EM_DASH not in opt.get("help", ""), f"{pid}.{opt['name']} help"
        for choice in opt.get("choices") or []:
            if isinstance(choice, dict):
                assert EM_DASH not in str(choice.get("label", "")), f"{pid}.{opt['name']} choice"


CONTRACT_DATA: dict[str, dict[str, dict[str, Any]]] = json.loads(
    CONTRACT.read_text(encoding="utf-8")
)


@pytest.mark.parametrize("pid", sorted(CONTRACT_DATA))
def test_saved_cells_keep_working(pid: str) -> None:
    manifest = dict(MANIFESTS).get(pid)
    assert manifest is not None, f"bundled plugin {pid} disappeared"
    current = {o["name"]: o for o in manifest.get("cell_options") or []}
    for name, pinned in CONTRACT_DATA[pid].items():
        assert name in current, f"{pid}: option {name!r} was renamed or removed"
        opt = current[name]
        assert opt.get("default") == pinned["default"], f"{pid}.{name}: default changed"
        if "choices" in pinned:
            values = [
                c.get("value") if isinstance(c, dict) else c for c in opt.get("choices") or []
            ]
            missing = [v for v in pinned["choices"] if v not in values]
            assert not missing, f"{pid}.{name}: choice values {missing} were renamed or removed"
