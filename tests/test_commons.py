"""app.commons: pictures from Wikimedia Commons, with every request answered here."""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path

import pytest

from app import commons

SHIP_CAT = {"query": {"categorymembers": [{"title": "Category:Maersk Elba (ship, 2011)"}]}}
SHIP_FILES = {
    "query": {
        "categorymembers": [
            {"title": "File:Maersk Elba IMO 9458054 Hamburg 2019.jpg"},
            {"title": "File:Maersk Elba builder's plaque.jpg"},
            {"title": "File:Harbour view with ships (12345678901).jpg"},
            {"title": "File:Elba plan.svg"},
            {"title": "Category:Maersk Elba in Rotterdam"},
        ]
    }
}


def _info(titles: list[str]) -> dict:
    pages = {}
    for i, t in enumerate(titles):
        if "plaque" in t:
            w, h = 2400, 1600
        elif "Harbour" in t:
            w, h = 3000, 2000
        elif "IMO" in t:
            w, h = 1800, 1200
        else:
            w, h = 500, 300
        pages[str(i)] = {
            "title": t,
            "imageinfo": [
                {
                    "thumburl": f"https://upload.wikimedia.org/thumb/{i}.jpg",
                    "descriptionurl": f"https://commons.wikimedia.org/wiki/{urllib.parse.quote(t)}",
                    "width": w,
                    "height": h,
                    "extmetadata": {
                        "Artist": {"value": '<a href="x">A. &amp; B. Spotter</a>'},
                        "LicenseShortName": {"value": "CC BY-SA 4.0"},
                    },
                }
            ],
        }
    return {"query": {"pages": pages}}


@pytest.fixture
def commons_offline(tmp_path: Path, monkeypatch):
    calls: list[str] = []

    def fake_http(url: str, accept: str = "application/json", timeout: float = 6.0):
        calls.append(url)
        if url.startswith("https://upload.wikimedia.org/"):
            return b"\xff\xd8\xff" + b"jpegbytes"
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if q.get("list") == "categorymembers":
            if q["cmtitle"] == "Category:IMO 9458054":
                return json.dumps(SHIP_CAT).encode()
            if q["cmtitle"].startswith("Category:Maersk Elba"):
                return json.dumps(SHIP_FILES).encode()
            return json.dumps({"query": {"categorymembers": []}}).encode()
        if q.get("list") == "search":
            return json.dumps(
                {"query": {"search": [{"title": "File:Hubble.jpg"}, {"title": "File:Notes.pdf"}]}}
            ).encode()
        if "titles" in q:
            titles = q["titles"].split("|")
            if titles == ["File:Nothing here.jpg"]:
                return json.dumps(
                    {"query": {"pages": {"-1": {"title": titles[0], "missing": ""}}}}
                ).encode()
            return json.dumps(_info(titles)).encode()
        return None

    monkeypatch.setattr(commons, "_http", fake_http)
    commons.configure(tmp_path / "commons", contact=lambda: "tester@example.org", version="0.465.0")
    yield calls
    commons.configure(None)


def test_user_agent_names_the_host_and_the_contact(commons_offline):
    assert (
        commons.user_agent()
        == "tesserae/0.465.0 (+https://github.com/dmellok/tesserae; tester@example.org)"
    )
    commons.configure(None, contact=lambda: "", version="")
    assert commons.user_agent() == "tesserae/dev (+https://github.com/dmellok/tesserae)"


def test_file_by_title_with_author_and_licence(commons_offline, tmp_path):
    calls = commons_offline
    pic = commons.file("HST-SM4.jpeg")
    assert (
        pic
        and pic["title"] == "File:HST-SM4.jpeg"
        and pic["src"].startswith("https://upload.wikimedia.org/thumb/")
    )
    assert pic["by"] == "A. & B. Spotter" and pic["lic"] == "CC BY-SA 4.0" and pic["w"] == 500, (
        "the HTML is stripped and unescaped"
    )
    n = len(calls)
    assert commons.file("File:HST-SM4.jpeg") == pic and len(calls) == n, "kept on disk; asked once"
    assert commons.file("Nothing here.jpg") is None and commons.file("") is None
    assert (tmp_path / "commons" / "titles").exists()


def test_portrait_descends_into_the_ships_category_and_ranks(commons_offline):
    calls = commons_offline
    pic = commons.portrait("IMO 9458054", name="Maersk Elba", ident="9458054", descend="(ship")
    assert pic and "IMO 9458054" in pic["title"], (
        "the file named with the number, not the larger plaque or harbour view"
    )
    assert len([c for c in calls if "categorymembers" in c]) == 2, (
        "the IMO category, then the ship's own"
    )
    n = len(calls)
    assert (
        commons.portrait("IMO 9458054", name="Maersk Elba", ident="9458054", descend="(ship") == pic
        and len(calls) == n
    )
    assert commons.portrait("IMO 0000000") is None
    n = len(calls)
    assert commons.portrait("IMO 0000000") is None and len(calls) == n, "a miss is remembered"


def test_title_score_prefers_portraits():
    s = commons.title_score
    assert s("File:Queen Mary 2 IMO 9241061 Nha Trang 2019.jpg", "9241061", "Queen Mary 2") > s(
        "File:Queen Mary 2 in Southampton.jpg", "9241061", "Queen Mary 2"
    )
    assert s("File:Queen Mary 2 in Southampton.jpg", "9241061", "Queen Mary 2") > s(
        "File:Courtesy Call Queen Mary 2 (5430332565).jpg", "9241061", "Queen Mary 2"
    )
    assert s("File:Queen Mary 2 builder's plaque.jpg", "", "Queen Mary 2") < 0


def test_search_fetch_data_uri_and_credit(commons_offline, tmp_path):
    found = commons.search("Hubble Space Telescope")
    assert [p["title"] for p in found] == ["File:Hubble.jpg"], "only picture files"
    raw = commons.fetch(found[0])
    assert raw and raw.startswith(b"\xff\xd8\xff")
    assert (tmp_path / "commons" / "bytes").exists()
    assert commons.data_uri(found[0]).startswith("data:image/jpeg;base64,")
    assert (
        commons.credit(found[0], "Nano Banana")
        == "painted by Nano Banana from a picture by A. & B. Spotter (Wikimedia Commons, CC BY-SA 4.0)"
    )
    assert (
        commons.credit(None) == "photo by a Commons contributor (Wikimedia Commons, open licence)"
    )
    size = commons.cache_size()
    assert size["files"] >= 1 and size["bytes"] > 0, "the bytes are on disk; a search is never kept"
    assert commons.purge() == size["files"] and commons.cache_size()["files"] == 0


def test_requests_keep_a_gap(monkeypatch, tmp_path):
    """Two requests in a row wait GAP_S between them, as Commons asks."""
    commons.configure(None)
    stamps: list[float] = []

    class Resp:
        def __init__(self) -> None:
            self.headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"{}"

    def fake_open(req, timeout=0):
        stamps.append(time.time())
        return Resp()

    monkeypatch.setattr(commons.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(commons, "GAP_S", 0.15)
    commons._LAST[0] = 0.0
    commons._http("https://commons.wikimedia.org/w/api.php?a=1")
    commons._http("https://commons.wikimedia.org/w/api.php?a=2")
    assert stamps[1] - stamps[0] >= 0.14
