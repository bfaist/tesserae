"""``show_if`` on widget cell options (app/option_conditions.py).

The grammar is small on purpose: every key must match, a value is an exact
match, a list is any-of, ``{"not": ...}`` is none-of, and a field whose
controller is hidden is hidden too. The rendered form must keep a hidden
field's input in place, enabled, so its value is still submitted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask, render_template

from app.main import REPO_ROOT, create_app
from app.option_conditions import option_visible

OPTS = [
    {
        "name": "layout",
        "type": "select",
        "label": "Layout",
        "default": "list",
        "choices": [{"value": v, "label": v} for v in ("list", "cluster", "bleed")],
    },
    {
        "name": "cluster_style",
        "type": "select",
        "label": "Style",
        "default": "sheet",
        "choices": [{"value": v, "label": v} for v in ("sheet", "blend")],
        "show_if": {"layout": "cluster"},
    },
    {
        "name": "cluster_every",
        "type": "select",
        "label": "Every",
        "default": "3",
        "choices": [{"value": "3", "label": "3"}],
        "show_if": {"cluster_style": "blend"},
    },
    {"name": "url", "type": "string", "label": "URL", "default": ""},
    {"name": "token", "type": "string", "label": "Token", "show_if": {"url": {"not": ""}}},
    {"name": "show_sci", "type": "boolean", "label": "Sci", "default": True},
    {"name": "sci_style", "type": "string", "label": "Sci style", "show_if": {"show_sci": True}},
    {
        "name": "pictures",
        "type": "select",
        "label": "Pictures",
        "default": "photo",
        "choices": [{"value": v, "label": v} for v in ("photo", "gen", "plate_gen")],
    },
    {
        "name": "art_style",
        "type": "string",
        "label": "Art",
        "show_if": {"pictures": ["gen", "plate_gen"]},
    },
]


def _spec(name: str) -> dict:
    return next(o for o in OPTS if o["name"] == name)


def test_no_condition_always_shows() -> None:
    assert option_visible(_spec("layout"), {}, OPTS)


def test_equals_uses_default_when_unset() -> None:
    assert not option_visible(_spec("cluster_style"), {}, OPTS)
    assert option_visible(_spec("cluster_style"), {"layout": "cluster"}, OPTS)


def test_any_of_list() -> None:
    assert option_visible(_spec("art_style"), {"pictures": "plate_gen"}, OPTS)
    assert not option_visible(_spec("art_style"), {"pictures": "photo"}, OPTS)


def test_not_empty() -> None:
    assert not option_visible(_spec("token"), {"url": ""}, OPTS)
    assert option_visible(_spec("token"), {"url": "http://birdnet.local"}, OPTS)


def test_boolean_matches_as_text() -> None:
    assert option_visible(_spec("sci_style"), {}, OPTS)
    assert not option_visible(_spec("sci_style"), {"show_sci": False}, OPTS)


def test_hidden_controller_hides_dependent() -> None:
    """cluster_every needs blend, but blend only counts while the cluster
    layout is picked: a stale blend under the list layout hides it."""
    assert not option_visible(
        _spec("cluster_every"), {"layout": "list", "cluster_style": "blend"}, OPTS
    )
    assert option_visible(
        _spec("cluster_every"), {"layout": "cluster", "cluster_style": "blend"}, OPTS
    )


def test_unknown_controller_is_ignored() -> None:
    spec = {"name": "x", "type": "string", "label": "X", "show_if": {"nope": "1"}}
    assert option_visible(spec, {}, [*OPTS, spec])


def test_cycle_hides_instead_of_recursing() -> None:
    a = {"name": "a", "type": "string", "label": "A", "show_if": {"b": ""}}
    b = {"name": "b", "type": "string", "label": "B", "show_if": {"a": ""}}
    assert not option_visible(a, {}, [a, b])


def test_multiselect_matches_any_pick() -> None:
    spec = {"name": "x", "type": "string", "label": "X", "show_if": {"tags": "b"}}
    tags = {"name": "tags", "type": "multiselect", "label": "T", "default": []}
    assert option_visible(spec, {"tags": ["a", "b"]}, [tags, spec])
    assert not option_visible(spec, {"tags": ["a"]}, [tags, spec])


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    return create_app(
        testing=True,
        data_root=tmp_path,
        plugins_dir=REPO_ROOT / "plugins",
        renderers_dir=REPO_ROOT / "renderers",
    )


def test_drawer_form_hides_but_keeps_inputs(app: Flask) -> None:
    with app.test_request_context():
        html = render_template(
            "panels_source_form.html", opts=OPTS, values={"layout": "list"}, sid="s1"
        )
    assert "data-opt-scope" in html
    # The hidden field is wrapped and hidden, and its input is still there,
    # named and not disabled, so the drawer still submits its value.
    start = html.index('name="opt_cluster_style"')
    wrap = html.rindex('<div class="opt-cond"', 0, start)
    assert " hidden>" in html[wrap:start]
    assert "disabled" not in html[wrap : start + 200]
    assert "&#34;layout&#34;" in html[wrap:start] or "&quot;layout&quot;" in html[wrap:start]


def test_drawer_form_shows_matching_field(app: Flask) -> None:
    with app.test_request_context():
        html = render_template(
            "panels_source_form.html", opts=OPTS, values={"layout": "cluster"}, sid="s1"
        )
    start = html.index('name="opt_cluster_style"')
    wrap = html.rindex('<div class="opt-cond"', 0, start)
    assert " hidden>" not in html[wrap:start]


def test_schema_accepts_show_if_and_rejects_bad_shapes() -> None:
    import json

    import jsonschema

    schema = json.loads((REPO_ROOT / "schema" / "plugin.schema.json").read_text(encoding="utf-8"))
    item = schema["properties"]["cell_options"]["items"]
    for good in (
        {"layout": "cluster"},
        {"pictures": ["gen", "plate_gen"]},
        {"url": {"not": ""}},
        {"show_sci": True},
    ):
        jsonschema.validate({"name": "x", "type": "string", "label": "X", "show_if": good}, item)
    for bad in ({}, {"layout": {"is": "x"}}, {"layout": [{"a": 1}]}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"name": "x", "type": "string", "label": "X", "show_if": bad}, item)
