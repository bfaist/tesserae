// Plain-node self-check for clampScale (no test framework in this repo).
// Run: node tests/clamp_check.mjs
// Mirrors the cases previously covered by test_smoke.py's
// test_scale_sliders_clamp_to_bounds before the clamp moved from
// server.py to client.js.
import assert from "node:assert/strict";
import {
  clampScale,
  clampToDay,
  columnStyle,
  computeRange,
  freeFrom,
  layoutColumns,
  planCrowds,
  localizedFull,
  minimalMapFor,
  pctSpan,
  readOptions,
  styleLabel,
} from "../client.js";

assert.equal(clampScale(undefined, 1.0, 0.7, 1.5), 1.0, "missing falls back to default");
assert.equal(clampScale(null, 1.0, 0.7, 1.5), 1.0, "null falls back to default");
assert.equal(clampScale("not-a-number", 1.0, 0.7, 1.5), 1.0, "unparsable falls back to default");
assert.equal(clampScale(1.3, 1.0, 0.7, 1.5), 1.3, "in-range value passes through");
assert.equal(clampScale(9, 1.0, 0.7, 1.5), 1.5, "above max clamps down");
assert.equal(clampScale(0.01, 1.0, 0.7, 1.5), 0.7, "below min clamps up");

// readOptions must pull from ctx.cell.options (the real ctx shape the
// composer sends) — not top-level ctx.options, which doesn't exist and
// would silently no-op every slider on this widget.
assert.deepEqual(
  readOptions({ cell: { options: { event_title_scale: 1.3 } } }),
  { event_title_scale: 1.3 },
  "reads from ctx.cell.options"
);
assert.deepEqual(readOptions({ options: { event_title_scale: 1.3 } }), {}, "ignores top-level ctx.options");
assert.deepEqual(readOptions({}), {}, "missing cell falls back to {}");

// computeRange: day_start_hour/day_end_hour overrides (-1 = auto-fit).
assert.deepEqual(computeRange([]), { start: 9, end: 18 }, "no events falls back to 9-18");
assert.deepEqual(
  computeRange([{ start: "2026-01-01T10:00:00", end: "2026-01-01T11:00:00" }]),
  { start: 9, end: 12 },
  "auto-fits with 1h padding around events"
);
assert.deepEqual(computeRange([], 0, 24), { start: 0, end: 24 }, "explicit 0/24 shows the whole day");
assert.deepEqual(
  computeRange([{ start: "2026-01-01T10:00:00", end: "2026-01-01T11:00:00" }], 0, -1),
  { start: 0, end: 12 },
  "start override pins start, end still auto-fits"
);
assert.deepEqual(computeRange([], 20, 21), { start: 20, end: 21 }, "inverted-looking override kept as given");
assert.deepEqual(computeRange([], 20, 20), { start: 20, end: 21 }, "equal start/end widened by 1h to stay renderable");

// clampToDay: calendar_core includes a multi-day event in "today"'s list
// whenever it overlaps today, even on a day that isn't its start/end —
// each such day must clamp to *that* day, not redraw at the event's
// original start hour.
const trip = { start: "2026-08-09T16:00:00", end: "2026-08-11T10:00:00" };
assert.deepEqual(clampToDay(trip, "2026-08-09"), { s: 16, e: 24 }, "start day keeps the real start hour, runs to midnight");
assert.deepEqual(clampToDay(trip, "2026-08-10"), { s: 0, e: 24 }, "a pass-through day runs the full 24h, not the start hour");
assert.deepEqual(clampToDay(trip, "2026-08-11"), { s: 0, e: 10 }, "end day starts at midnight, keeps the real end hour");
assert.deepEqual(
  clampToDay({ start: "2026-08-09T16:00:00", end: "2026-08-09T17:00:00" }, "2026-08-09"),
  { s: 16, e: 17 },
  "single-day event is unaffected"
);

// #248: an event living wholly on another day must not clamp at all. The
// pass-through rule above reads "neither edge is today" as "runs straight
// through today", so tomorrow's 10-11 used to paint the entire column while
// its label still read 10:00-11:00 — which looked like a stale cached copy
// of an event that had been moved to tomorrow.
const tomorrow = { start: "2026-08-22T10:00:00", end: "2026-08-22T11:00:00" };
assert.equal(clampToDay(tomorrow, "2026-08-21"), null, "an event entirely tomorrow does not draw today");
assert.equal(clampToDay(tomorrow, "2026-08-23"), null, "an event entirely yesterday does not draw today");
assert.deepEqual(clampToDay(tomorrow, "2026-08-22"), { s: 10, e: 11 }, "on its own day it draws normally");
// The multi-day trip must survive the new guard on every day it occupies.
assert.deepEqual(clampToDay(trip, "2026-08-10"), { s: 0, e: 24 }, "pass-through day still clamps, not rejected");
assert.equal(clampToDay(trip, "2026-08-12"), null, "the day after a trip ends draws nothing");

// pctSpan: a block's [s,e) hour span must clamp to the visible lane
// without ever exceeding 100% height, even when day_start_hour/
// day_end_hour narrows the range well below a pass-through day's full
// 0-24h span (regression: height used to be derived from the
// *unclamped* span, so it could exceed 100% and spill past the lane's
// bottom edge).
assert.deepEqual(pctSpan(9, 17, 8, 10), { top: 10, height: 80 }, "fully inside the range renders normally");
assert.deepEqual(pctSpan(0, 24, 8, 4), { top: 0, height: 100 }, "a full 0-24h pass-through day clamps to exactly the lane, not beyond");
assert.deepEqual(pctSpan(20, 24, 8, 4), { top: 100, height: 2 }, "a span entirely after the visible range clamps to the bottom edge, not off it");

// date_label_style: full / short / minimal (1-2 chars, disambiguated).
const DOW_MINIMAL = { Tuesday: "Tu", Thursday: "Th" };
assert.equal(styleLabel("Monday", "full", {}), "MONDAY", "full keeps the whole word, upper-cased");
assert.equal(styleLabel("Tuesday", "short", {}), "TUE", "short is a 3-letter abbreviation");
assert.equal(styleLabel("Tuesday", "minimal", DOW_MINIMAL), "TU", "minimal uses the disambiguation map");
assert.equal(styleLabel("Thursday", "minimal", DOW_MINIMAL), "TH", "Tue/Thu don't collide at 1 char");
assert.equal(styleLabel("Zzyzx", "minimal", {}), "ZZ", "unmapped label falls back to first 2 chars");

// locales contract (docs/widgets.md#locales-strings): localizedFull() /
// minimalMapFor() are what feed a locale-aware name into styleLabel above.
// A Monday, so weekday indices are unambiguous either way.
const monday = new Date(2026, 0, 5);
assert.equal(localizedFull(monday, "weekday", "en"), "Monday", "English reads the hardcoded array, not Intl");
assert.equal(localizedFull(monday, "month", "en-US"), "January", "English variants (en-US) still count as English");
assert.equal(localizedFull(monday, "weekday", "fr"), "lundi", "non-English asks Intl for the real localised name");
assert.equal(localizedFull(monday, "month", "fr"), "janvier", "same for months");
assert.equal(localizedFull(monday, "weekday", ""), "Monday", "no locale at all defaults to English");

assert.equal(minimalMapFor("weekday", "en").Tuesday, "Tu", "English gets the real disambiguation map");
assert.deepEqual(minimalMapFor("weekday", "fr"), {}, "non-English gets {} -- styleLabel's generic slice, not English abbreviations");
assert.deepEqual(minimalMapFor("month", "de"), {}, "same for months");

console.log("clamp_check.mjs: all assertions passed");

// layoutColumns: overlapping events share the lane side by side (#345).
assert.deepEqual(layoutColumns([]), [], "no events");
assert.deepEqual(layoutColumns([{ s: 9, e: 10 }]), [{ col: 0, n: 1, span: 1, run: 0 }], "one event keeps the lane");
assert.deepEqual(
  layoutColumns([{ s: 9, e: 10 }, { s: 10, e: 11 }]),
  [{ col: 0, n: 1, span: 1, run: 0 }, { col: 0, n: 1, span: 1, run: 1 }],
  "end-to-end events do not overlap"
);
// The report: short meetings at 15:30-16:00 over a long event from 15:45.
const reported = layoutColumns([{ s: 15.5, e: 16 }, { s: 15.5, e: 16 }, { s: 15.75, e: 16 }, { s: 15.75, e: 19.5 }]);
assert.deepEqual(reported.map((x) => x.n), [4, 4, 4, 4], "four overlapping events need four columns");
assert.deepEqual(new Set(reported.map((x) => x.col)).size, 4, "each takes its own column");
// An event widens into free columns to its right.
const widen = layoutColumns([{ s: 9, e: 12 }, { s: 9, e: 10 }, { s: 13, e: 14 }]);
assert.deepEqual(widen[0], { col: 0, n: 2, span: 1, run: 0 }, "the long one keeps column 0");
assert.deepEqual(widen[1], { col: 1, n: 2, span: 1, run: 0 }, "the short one sits beside it");
assert.deepEqual(widen[2], { col: 0, n: 1, span: 1, run: 1 }, "a later event starts a new run with the whole lane");
const chain = layoutColumns([{ s: 9, e: 10 }, { s: 9.5, e: 11 }, { s: 10, e: 12 }]);
assert.deepEqual(chain.map((x) => [x.col, x.n, x.span]), [[0, 2, 1], [1, 2, 1], [0, 2, 1]], "a chain reuses column 0 once it is free");
const free = layoutColumns([{ s: 9, e: 10 }, { s: 9, e: 10 }, { s: 10.5, e: 11 }, { s: 9, e: 12 }]);
assert.deepEqual(free[2], { col: 1, n: 3, span: 2, run: 0 }, "an event widens across the free columns on its right");
assert.equal(columnStyle({ col: 0, n: 1, span: 1 }), "", "a lone event takes no inline placement");
assert.equal(columnStyle({ col: 1, n: 2, span: 1 }), "left:calc(50.000% + var(--space-1));right:auto;width:calc(50.000% - var(--space-1) - var(--space-1));");
assert.equal(columnStyle({ col: 0, n: 2, span: 1 }), "left:calc(0.000% + var(--space-1));right:auto;width:calc(50.000% - var(--space-1));");
// freeFrom: where an event has the lane to itself again.
assert.deepEqual(freeFrom([{ s: 9, e: 10 }]), [null], "nothing overlaps: nothing to wait for");
assert.deepEqual(freeFrom([{ s: 15.5, e: 16 }, { s: 15.75, e: 19.5 }]), [null, 16], "the long one is alone from 16:00; the short one never outlasts it");
assert.deepEqual(freeFrom([{ s: 9, e: 12 }, { s: 10, e: 11 }, { s: 11.5, e: 13 }]), [null, null, 12], "an event outlasted by one that overlaps it is never alone; the last one is alone from 12:00");
// planCrowds: a run too dense for its lane becomes one listed block.
const dense = [{ s: 15.5, e: 16 }, { s: 15.5, e: 16 }, { s: 15.75, e: 16 }, { s: 15.75, e: 19.5 }];
const dcols = layoutColumns(dense), dfree = freeFrom(dense);
assert.deepEqual(planCrowds(dense, dcols, dfree, 0).crowds, [], "no layout yet: columns stand");
assert.deepEqual(planCrowds(dense, dcols, dfree, 1000).crowds, [], "a wide lane keeps columns");
const narrow = planCrowds(dense, dcols, dfree, 280);
assert.deepEqual(narrow.crowds, [{ run: 0, s: 15.5, e: 16, members: [0, 1, 2] }], "four events in 280px: the three short ones are listed");
assert.deepEqual(narrow.start, [null, null, null, 16], "the long one draws from the crowd's end");
const two = layoutColumns([{ s: 9, e: 10 }, { s: 9, e: 10 }]);
assert.deepEqual(planCrowds([{ s: 9, e: 10 }, { s: 9, e: 10 }], two, freeFrom([{ s: 9, e: 10 }, { s: 9, e: 10 }]), 200).crowds, [], "two events are never a crowd");
console.log("calendar_day client checks passed");
