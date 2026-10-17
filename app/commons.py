"""Free pictures from Wikimedia Commons, for every widget that paints.

One lookup for the host: a file by its title, a category's files, the best
portrait of a subject in a category, the picture's bytes for the painter and
one wording for the credit. Everything is kept under ``<data root>/commons/``
so a subject two widgets show is looked up once, every request carries one
User-Agent with the operator's contact (as Wikimedia asks), and requests go
one at a time with a small gap between them (as Commons asks of a client).

The module is configured once by the app factory (:func:`configure`); a
widget on an older host that lacks it keeps its own copy, as with
``app.artwork``::

    try:
        from app import commons
    except ImportError:
        commons = None

Every URL fetched here is Commons's own, so the SSRF guard for user-supplied
URLs does not apply.
"""

from __future__ import annotations

import contextlib
import hashlib
import html
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

API = "https://commons.wikimedia.org/w/api.php"
HIT_S = 30 * 86400  # a found picture is kept this long
MISS_S = 7 * 86400  # a subject without one is asked again after this
BYTES_S = 90 * 86400
TIMEOUT_S = 6.0
MAX_BYTES = 12_000_000
GAP_S = 0.25  # between requests to Commons
THUMB_PX = 1400
NOT_A_PORTRAIT: tuple[str, ...] = (
    "plaque",
    "plate",
    "interior",
    "cabin",
    "deck",
    "bridge",
    "restaurant",
    "lobby",
    "atrium",
    "pool",
    "lounge",
    "theatre",
    "theater",
    "model",
    "logo",
    "map",
    "plan",
    "drawing",
    "diagram",
    "engine",
    "propeller",
    "anchor",
    "bell",
    "funnel",
    "detail",
    "crew",
    "captain",
    "menu",
    "ticket",
    "poster",
    "stamp",
    "crop",
    "closeup",
    "close-up",
    "inside",
    "stair",
    "corridor",
    "suite",
    "room",
    "on board",
    "aboard",
    "onboard",
    "from the deck",
    "view from",
    "seen from",
    "wake of",
    "visit",
    "courtesy",
    "ceremony",
    "christening",
    "naming",
    "launch",
    "lifeboat",
    "gangway",
    "passenger",
    "people",
    "tour",
    "night",
    "fireworks",
    "sign",
    "bow of",
    "stern of",
    "hull of",
    "part of",
)

_STATE: dict[str, Any] = {"dir": None, "contact": None, "version": ""}
_LOCK = threading.Lock()
_LAST = [0.0]


def configure(
    folder: Path | None, contact: Callable[[], str] | None = None, version: str = ""
) -> None:
    """Where the cache lives and how to find the operator's contact; called
    once by the app factory. Without a folder nothing is kept on disk."""
    _STATE["dir"] = Path(folder) if folder else None
    _STATE["contact"] = contact
    _STATE["version"] = version


def user_agent() -> str:
    contact = ""
    fn = _STATE.get("contact")
    if fn is not None:
        with contextlib.suppress(Exception):
            contact = " ".join(str(fn() or "").split())
    v = _STATE.get("version") or "dev"
    return f"tesserae/{v} (+https://github.com/dmellok/tesserae{'; ' + contact if contact else ''})"


# ----------------------------------------------------------------------
# The cache
# ----------------------------------------------------------------------
def _path(kind: str, key: str, suffix: str = ".json") -> Path | None:
    folder = _STATE.get("dir")
    if folder is None:
        return None
    digest = hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:24]
    return Path(folder) / kind / f"{digest}{suffix}"


def _read(kind: str, key: str, max_age: float) -> Any:
    p = _path(kind, key)
    if p is None:
        return None
    try:
        got = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(got, dict) or time.time() - float(got.get("at") or 0) > max_age:
        return None
    return got.get("value")


def _write(kind: str, key: str, value: Any) -> None:
    p = _path(kind, key)
    if p is None:
        return
    with contextlib.suppress(OSError):
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"at": time.time(), "key": key, "value": value}), encoding="utf-8"
        )
        tmp.replace(p)


def cache_size() -> dict[str, int]:
    """{files, bytes} of what is kept, for the settings page."""
    folder = _STATE.get("dir")
    out = {"files": 0, "bytes": 0}
    if folder is None or not Path(folder).exists():
        return out
    for p in Path(folder).rglob("*"):
        if p.is_file():
            out["files"] += 1
            with contextlib.suppress(OSError):
                out["bytes"] += p.stat().st_size
    return out


def purge() -> int:
    folder = _STATE.get("dir")
    n = 0
    if folder is None:
        return 0
    for p in Path(folder).rglob("*"):
        if p.is_file():
            with contextlib.suppress(OSError):
                p.unlink()
                n += 1
    return n


# ----------------------------------------------------------------------
# HTTP, one request at a time
# ----------------------------------------------------------------------
def _http(url: str, accept: str = "application/json", timeout: float = TIMEOUT_S) -> bytes | None:
    """GET as this host; None on any failure (the reason is logged, not raised)."""
    with _LOCK:
        wait = GAP_S - (time.time() - _LAST[0])
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": user_agent(), "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as err:
            if err.code == 429:
                with contextlib.suppress(ValueError, TypeError):
                    _LAST[0] = time.time() + min(30.0, float(err.headers.get("Retry-After") or 5))
            logger.info("commons: HTTP %s for %s", err.code, url[:120])
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            logger.info("commons: %s for %s", type(err).__name__, url[:120])
            return None
        finally:
            _LAST[0] = max(_LAST[0], time.time())
    return data if len(data) <= MAX_BYTES else None


def _api(params: dict[str, str]) -> Any:
    q = urllib.parse.urlencode({"action": "query", "format": "json", **params})
    raw = _http(f"{API}?{q}")
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None


def _plain(text: str) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    s = " ".join(s.split())
    if s.lower().startswith("unknown author"):
        return "an unknown author"
    return s


def _picture(page: dict[str, Any]) -> dict[str, Any] | None:
    ii = (page.get("imageinfo") or [{}])[0]
    if not ii.get("thumburl"):
        return None
    ex = ii.get("extmetadata") or {}
    return {
        "title": str(page.get("title") or ""),
        "src": str(ii["thumburl"]),
        "link": str(ii.get("descriptionurl") or ""),
        "by": _plain(str((ex.get("Artist") or {}).get("value") or ""))[:80],
        "lic": str((ex.get("LicenseShortName") or {}).get("value") or "")[:40],
        "w": int(ii.get("width") or 0),
        "h": int(ii.get("height") or 0),
    }


# ----------------------------------------------------------------------
# Lookups
# ----------------------------------------------------------------------
def file(title: str, *, px: int = THUMB_PX) -> dict[str, Any] | None:
    """A picture by its Commons title (``File:…``): {title, src, link, by,
    lic, w, h}, or None when there is no such file or Commons is unreachable."""
    title = str(title or "").strip()
    if not title:
        return None
    if not title.lower().startswith("file:"):
        title = "File:" + title
    key = f"{title}|{px}"
    got = _read("titles", key, HIT_S)
    if got is not None:
        return got or None
    data = _api(
        {
            "titles": title,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|size",
            "iiurlwidth": str(px),
        }
    )
    if data is None:
        return None
    pic = None
    with contextlib.suppress(KeyError, TypeError, AttributeError):
        for page in data["query"]["pages"].values():
            pic = _picture(page)
            if pic:
                break
    _write("titles", key, pic or {})
    return pic


def files(titles: Iterable[str], *, px: int = THUMB_PX) -> list[dict[str, Any]]:
    """Several pictures by title in one request (up to fifty), in no particular order."""
    wanted = [t if t.lower().startswith("file:") else "File:" + t for t in titles if t][:50]
    if not wanted:
        return []
    data = _api(
        {
            "titles": "|".join(wanted),
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|size",
            "iiurlwidth": str(px),
        }
    )
    out: list[dict[str, Any]] = []
    with contextlib.suppress(KeyError, TypeError, AttributeError):
        for page in data["query"]["pages"].values():
            pic = _picture(page)
            if pic:
                out.append(pic)
    return out


def category(name: str, *, limit: int = 120) -> tuple[list[str], list[str]]:
    """(file titles, subcategory titles) of a category, kept a week."""
    name = str(name or "").strip()
    if not name:
        return [], []
    if not name.lower().startswith("category:"):
        name = "Category:" + name
    key = f"{name}|{limit}"
    got = _read("categories", key, MISS_S)
    if isinstance(got, dict):
        return list(got.get("files") or []), list(got.get("subcats") or [])
    data = _api(
        {
            "list": "categorymembers",
            "cmtitle": name,
            "cmtype": "file|subcat",
            "cmlimit": str(min(500, max(1, limit))),
        }
    )
    if data is None:
        return [], []
    found: list[str] = []
    subcats: list[str] = []
    with contextlib.suppress(KeyError, TypeError, AttributeError):
        for m in data["query"]["categorymembers"]:
            title = str(m.get("title") or "")
            if title.lower().endswith((".jpg", ".jpeg", ".png")):
                found.append(title)
            elif title.startswith("Category:"):
                subcats.append(title)
    _write("categories", key, {"files": found, "subcats": subcats})
    return found, subcats


def title_score(
    title: str, ident: str = "", name: str = "", avoid: tuple[str, ...] = NOT_A_PORTRAIT
) -> float:
    """How likely a file, by its name alone, is a portrait of the subject: an
    identifier (an IMO number, a registration) in the name first, then the
    subject's name, a year counts a little, a long upload id against, and the
    words of a plaque, a cabin or a visit count against."""
    t = str(title or "").lower()
    score = 0.0
    if ident and str(ident).lower() in t:
        score += 2.0
    words = [w for w in str(name or "").lower().split() if len(w) > 2]
    if words and all(w in t for w in words):
        score += 1.0
    elif any(len(w) > 3 and w in t for w in words):
        score += 0.4
    if re.search(r"\b(19|20)\d\d\b", t):
        score += 0.3
    if re.search(r"\(\d{8,}\)", t):
        score -= 0.5
    if any(bad in t for bad in avoid):
        score -= 3.0
    return score


def portrait(
    cat: str,
    *,
    name: str = "",
    ident: str = "",
    avoid: tuple[str, ...] = NOT_A_PORTRAIT,
    min_px: int = 640,
    px: int = THUMB_PX,
    descend: str = "",
) -> dict[str, Any] | None:
    """The best photograph of a subject in a category: the files are ranked by
    name, the best dozen asked for, and a landscape picture of a good size
    wins. When the category holds only a subcategory (Commons files a ship
    under ``Category:IMO <n>`` → ``Category:<Name> (ship, <year>)``), the
    first one whose name contains ``descend`` (or the first) is read instead.
    A miss is remembered a week, a hit a month."""
    cat = str(cat or "").strip()
    if not cat:
        return None
    key = f"{cat}|{name}|{ident}|{min_px}|{px}|{descend}"
    got = _read("portraits", key, HIT_S)
    if got is not None:
        return got or None
    miss = _read("portraits-miss", key, MISS_S)
    if miss is not None:
        return None
    titles, subcats = category(cat)
    if not titles and subcats:
        deeper = next((c for c in subcats if descend and descend.lower() in c.lower()), subcats[0])
        titles, _more = category(deeper)
    ranked = sorted(titles, key=lambda t: -title_score(t, ident, name, avoid))[:12]
    best: tuple[float, dict[str, Any]] | None = None
    for pic in files(ranked, px=px) if ranked else []:
        w, h = pic["w"], pic["h"]
        if w < min_px or h < min_px * 9 // 16:
            continue
        ratio = w / max(h, 1)
        score = (
            title_score(pic["title"], ident, name, avoid)
            + min(ratio, 2.2)
            + (0.5 if 1.2 <= ratio <= 1.9 else 0)
            + min(w, 4000) / 8000
        )
        if best is None or score > best[0]:
            best = (score, pic)
    if best:
        _write("portraits", key, best[1])
        return best[1]
    if titles or subcats or _STATE.get("dir") is None:
        _write("portraits-miss", key, {"none": True})
    return None


def search(query: str, *, limit: int = 8, px: int = THUMB_PX) -> list[dict[str, Any]]:
    """Files matching a search, with their details: for a tool that builds a
    curated table, not for a page at render time (search by name is unreliable
    for anything with a number in it)."""
    data = _api(
        {
            "list": "search",
            "srsearch": str(query or ""),
            "srnamespace": "6",
            "srlimit": str(min(50, max(1, limit))),
        }
    )
    titles: list[str] = []
    with contextlib.suppress(KeyError, TypeError, AttributeError):
        titles = [
            str(h["title"])
            for h in data["query"]["search"]
            if str(h.get("title", "")).lower().endswith((".jpg", ".jpeg", ".png"))
        ]
    return files(titles, px=px) if titles else []


# ----------------------------------------------------------------------
# The bytes, and the credit
# ----------------------------------------------------------------------
def fetch(picture: dict[str, Any] | str) -> bytes | None:
    """The picture's bytes (the thumbnail at the width asked for), kept on disk
    by URL; None when it cannot be had. A failed fetch is not remembered."""
    url = picture if isinstance(picture, str) else str((picture or {}).get("src") or "")
    if not url:
        return None
    p = _path("bytes", url, suffix=".bin")
    if p is not None and p.exists():
        with contextlib.suppress(OSError):
            if time.time() - p.stat().st_mtime < BYTES_S:
                return p.read_bytes()
    raw = _http(url, accept="image/*", timeout=20.0)
    if not raw:
        return None
    if p is not None:
        with contextlib.suppress(OSError):
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(raw)
            tmp.replace(p)
    return raw


def data_uri(picture: dict[str, Any] | str) -> str:
    """The picture inline, for a painter that cannot fetch Commons itself."""
    raw = fetch(picture)
    if not raw:
        return ""
    import base64

    kind = "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return f"data:{kind};base64," + base64.b64encode(raw).decode()


def credit(picture: dict[str, Any] | None, painted_by: str = "") -> str:
    """One wording: "painted by X from a picture by Y (Wikimedia Commons, CC
    BY-SA 4.0)", or "photo by Y (Wikimedia Commons, …)" without a painter."""
    by = str((picture or {}).get("by") or "a Commons contributor")
    lic = str((picture or {}).get("lic") or "open licence")
    source = f"a picture by {by} (Wikimedia Commons, {lic})"
    return (
        f"painted by {painted_by} from {source}"
        if painted_by
        else f"photo by {by} (Wikimedia Commons, {lic})"
    )
