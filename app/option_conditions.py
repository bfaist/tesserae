"""``show_if`` on widget cell options: show a field only when it applies.

A cell option can declare ``show_if``, an object keyed by other option
names. Every key must match for the field to show::

    "show_if": {"layout": "cluster"}                     equals
    "show_if": {"pictures": ["gen", "plate_gen"]}        any of
    "show_if": {"url": {"not": ""}}                      none of (here: set)

Values compare as strings, so ``true`` matches a ticked switch and ``"8"``
matches 8. A multiselect matches when any of its picks matches. A field
whose controlling option is itself hidden is hidden too, so a chain like
"cluster style only for the cluster layout, refresh only for the blend
style" needs no repetition.

Hiding is presentation only. A hidden field stays in the form and is
submitted with its value, so switching back restores what was there and
the widget's saved options never lose a key. This module answers the
first render on the server (so nothing flashes); ``static/components.js``
(``attachShowIf``) keeps the form in step as the operator edits.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# Deeper than any real manifest; stops a cycle (a shows-if b shows-if a)
# from recursing forever. A cycle evaluates as hidden.
_MAX_DEPTH = 8


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _matches(actual: Any, expected: Any) -> bool:
    """One ``show_if`` entry against the controlling option's value."""
    if isinstance(expected, Mapping) and "not" in expected:
        return not _matches(actual, expected["not"])
    wanted = (
        {_as_text(v) for v in expected}
        if isinstance(expected, list | tuple)
        else {_as_text(expected)}
    )
    if isinstance(actual, list | tuple):
        return any(_as_text(v) in wanted for v in actual)
    return _as_text(actual) in wanted


def option_visible(
    spec: Mapping[str, Any],
    values: Mapping[str, Any] | None,
    specs: Iterable[Mapping[str, Any]],
    _depth: int = 0,
) -> bool:
    """Whether ``spec`` shows given the cell's current ``values``.

    ``specs`` is the widget's full option list, used for defaults of
    options the stored values don't carry yet and for the controlling
    options' own conditions. A ``show_if`` naming an option the widget
    doesn't have is ignored rather than hiding the field for good."""
    cond = spec.get("show_if")
    if not isinstance(cond, Mapping) or not cond:
        return True
    if _depth >= _MAX_DEPTH:
        return False
    by_name = {s.get("name"): s for s in specs if isinstance(s, Mapping)}
    current = values or {}
    for name, expected in cond.items():
        controller = by_name.get(name)
        if controller is None:
            continue
        if not option_visible(controller, current, by_name.values(), _depth + 1):
            return False
        actual = current[name] if name in current else controller.get("default")
        if not _matches(actual, expected):
            return False
    return True
