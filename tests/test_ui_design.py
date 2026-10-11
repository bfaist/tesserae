"""Paper, the default design, and the classic one: the header switch, the
route it posts to, and what the base template renders for each setting."""

from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app.main import REPO_ROOT, create_app


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    a = create_app(
        testing=True,
        data_root=tmp_path,
        plugins_dir=REPO_ROOT / "plugins",
        renderers_dir=REPO_ROOT / "renderers",
        devices_dir=REPO_ROOT / "devices",
    )
    a.config["TESTING"] = True
    return a


def _sign_in(client: FlaskClient) -> None:
    client.post("/setup", data={"password": "abcdefgh", "password_confirm": "abcdefgh"})


def _ui(app: Flask) -> object:
    return (app.config["SETTINGS_STORE"].get_section("app") or {}).get("ui_design")


def test_paper_by_default_with_switch_offered(app: Flask) -> None:
    """An install that never touched the switch gets Paper (the default since
    0.449), with the switch showing it on and offering the classic design."""
    client = app.test_client()
    _sign_in(client)
    assert _ui(app) is None  # the default is not written as a choice
    body = client.get("/history").get_data(as_text=True)
    assert '<html lang="en" data-ui="paper">' in body
    assert 'aria-checked="true"' in body
    assert "Go back to the classic design" in body
    assert "style/paper.css" in body
    # Both designs draw the same server mark and lockup.
    assert '<span class="brand-mark" aria-hidden="true"><svg viewBox="0 0 256 256"' in body
    assert 'class="brand-edge"' in body
    assert '<span class="brand-tag">Server</span>' in body


def test_switch_on_then_off_returns_to_the_page(app: Flask) -> None:
    client = app.test_client()
    _sign_in(client)
    resp = client.post("/settings/ui-design", data={"ui_design": "paper", "next": "/history"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/history")
    assert _ui(app) == "paper"
    body = client.get("/history").get_data(as_text=True)
    assert '<html lang="en" data-ui="paper">' in body
    assert 'aria-checked="true"' in body
    assert '<span class="brand-tag">Server</span>' in body  # the self-hosted lockup

    client.post("/settings/ui-design", data={"ui_design": "classic", "next": "/history"})
    assert _ui(app) == "classic"
    assert '<html lang="en" data-ui="paper">' not in client.get("/history").get_data(as_text=True)


def test_unknown_value_falls_back_to_classic(app: Flask) -> None:
    client = app.test_client()
    _sign_in(client)
    client.post("/settings/ui-design", data={"ui_design": "<script>"})
    assert _ui(app) == "classic"


def test_next_must_be_a_local_path(app: Flask) -> None:
    client = app.test_client()
    _sign_in(client)
    resp = client.post(
        "/settings/ui-design", data={"ui_design": "paper", "next": "//evil.example/x"}
    )
    assert "evil.example" not in resp.headers["Location"]


def test_switch_requires_sign_in(tmp_path: Path) -> None:
    """With the auth gate installed (testing=False), an anonymous post
    changes nothing."""
    gated = create_app(
        testing=False,
        data_root=tmp_path,
        plugins_dir=REPO_ROOT / "plugins",
        renderers_dir=REPO_ROOT / "renderers",
    )
    gated.config["TESTING"] = True
    _sign_in(gated.test_client())  # sets the password; this client's session is dropped
    resp = gated.test_client().post("/settings/ui-design", data={"ui_design": "paper"})
    assert _ui(gated) != "paper"
    assert resp.status_code in (302, 401, 403)
