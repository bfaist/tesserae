"""``Plugin.has_admin`` drives the "Open admin" links. It must mean the
plugin serves a page at /plugins/<id>/, not merely that it exports a
blueprint (tesserae-widget-trmnl-proxy#1: a blueprint with only
``/image`` linked to a 404)."""

from __future__ import annotations

import types
from pathlib import Path

from flask import Blueprint, Flask

from app.plugin_loader import Plugin, PluginRegistry, register_routes


def _plugin(tmp_path: Path, plugin_id: str, route: str) -> Plugin:
    def blueprint() -> Blueprint:
        bp = Blueprint(f"{plugin_id}_bp", __name__)
        bp.add_url_rule(route, f"{plugin_id}_view", lambda: "ok")
        return bp

    module = types.SimpleNamespace(blueprint=blueprint)
    return Plugin(
        id=plugin_id,
        path=tmp_path,
        manifest={"id": plugin_id, "name": plugin_id},
        data_dir=tmp_path,
        server_module=module,  # type: ignore[arg-type]
    )


def test_admin_link_only_for_plugins_that_serve_an_index(tmp_path: Path) -> None:
    app = Flask(__name__)
    with_page = _plugin(tmp_path, "with_page", "/")
    image_only = _plugin(tmp_path, "image_only", "/image")
    register_routes(app, PluginRegistry(plugins={p.id: p for p in (with_page, image_only)}))

    with app.app_context():
        assert with_page.has_admin is True
        assert image_only.has_admin is False
    # Both still have a blueprint mounted (uninstall needs to know).
    assert with_page.has_blueprint and image_only.has_blueprint
    assert app.test_client().get("/plugins/image_only/image").status_code == 200
