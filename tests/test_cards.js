/*
 * Card render tests — Node's built-in runner, zero dependencies.
 *
 *   node --test tests/
 *
 * The card's renderers are pure string builders, so they are unit-testable
 * without a DOM. What is worth pinning here is escaping (these strings are
 * built with innerHTML, so an unescaped team name is an XSS hole) and the
 * score/leader logic, which is what the whole card exists to show.
 *
 * The DOM plumbing — overlays, delegated listeners, the fingerprint guard —
 * is not covered; that needs a real browser and is verified by hand.
 */

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

// The card classes are DECLARED at module load, and `class X extends
// HTMLElement` needs the base to exist even though nothing here instantiates
// one. A bare stub is enough; `customElements` stays undefined, which is what
// keeps the registration block from running.
global.HTMLElement = class {};

const cards = require(
  path.join(__dirname, "..", "custom_components", "yahoo_fantasy_football", "yahoo-fantasy-football-cards.js")
);

const { escapeHtml, fmtPoints, fmtDelta, renderMatchupRow, renderBanner, renderRosterSide, renderHistory } = cards;

const ROW = {
  matchup_id: "w13.m1",
  index: 1,
  home: { team_id: "1", name: "Tesla", points: 180.67, projected: 138.49 },
  away: { team_id: "5", name: "Your daddy", points: 104.09, projected: 126.61 },
  leader: "1",
};

/* ----------------------------------------------------------- formatting */

test("points render with two decimals like Yahoo", () => {
  assert.strictEqual(fmtPoints(180.67), "180.67");
  assert.strictEqual(fmtPoints(0), "0.00");
  assert.strictEqual(fmtPoints("12.5"), "12.50");
});

test("a genuinely absent score is an em dash, not a zero", () => {
  // Zero points and "no data" are different things and must not look alike.
  for (const empty of [null, undefined, "", "abc"]) {
    assert.strictEqual(fmtPoints(empty), "—");
  }
});

test("deltas carry an explicit plus sign", () => {
  assert.strictEqual(fmtDelta(6.4), "+6.40");
  assert.strictEqual(fmtDelta(-2.1), "-2.10");
  assert.strictEqual(fmtDelta("nope"), "");
});

/* -------------------------------------------------------------- escaping */

test("html metacharacters are escaped", () => {
  assert.strictEqual(escapeHtml(`<img src=x onerror=alert(1)>`), "&lt;img src=x onerror=alert(1)&gt;");
  assert.strictEqual(escapeHtml(`"&'`), "&quot;&amp;&#39;");
  assert.strictEqual(escapeHtml(null), "");
});

test("a hostile team name cannot inject markup", () => {
  // Team names are user-supplied and rendered via innerHTML.
  const row = { ...ROW, home: { ...ROW.home, name: `<script>alert(1)</script>` } };
  const html = renderMatchupRow(row);
  assert.ok(!html.includes("<script>"), "raw script tag leaked into the row");
  assert.ok(html.includes("&lt;script&gt;"));
});

test("a hostile play description cannot inject markup", () => {
  const html = renderBanner({ text: `<img onerror=alert(1)>`, delta: 6, correction: false }, "Last play");
  assert.ok(!html.includes("<img"), "raw img tag leaked into the banner");
});

test("a hostile player name cannot inject markup", () => {
  const html = renderRosterSide({
    name: "T",
    points: 1,
    starters: [{ slot: "QB", name: `<b>x</b>`, game: "", projected: 1, points: 1, stat_line: "", game_state: "post" }],
    bench: [],
  });
  assert.ok(!html.includes("<b>x</b>"));
});

/* ------------------------------------------------------------ score rows */

test("a matchup row shows both scores and both projections", () => {
  const html = renderMatchupRow(ROW);
  for (const expected of ["Tesla", "Your daddy", "180.67", "104.09", "138.49", "126.61"]) {
    assert.ok(html.includes(expected), `missing ${expected}`);
  }
});

test("the leader is marked exactly once per row", () => {
  const html = renderMatchupRow(ROW);
  // One team block + one score span carry the leader class.
  assert.strictEqual((html.match(/ffl-leader/g) || []).length, 2);
});

test("a tie marks nobody as leader", () => {
  const html = renderMatchupRow({ ...ROW, leader: null });
  assert.ok(!html.includes("ffl-leader"));
});

test("a row carries the identifiers the click handler reads", () => {
  const html = renderMatchupRow(ROW);
  assert.ok(html.includes('data-matchup-index="1"'));
  assert.ok(html.includes('data-matchup-id="w13.m1"'));
  assert.ok(html.includes('role="button"'), "rows must be keyboard reachable");
  assert.ok(html.includes('tabindex="0"'));
});

/* --------------------------------------------------------------- banner */

test("the banner shows the play text and its delta", () => {
  const html = renderBanner({ text: "Nacua 24 Yd TD", delta: 6.4, correction: false }, "Last play");
  assert.ok(html.includes("Nacua 24 Yd TD"));
  assert.ok(html.includes("+6.40"));
  assert.ok(html.includes("data-history"), "the banner must open the history overlay");
});

test("a correction is styled differently from a score", () => {
  const scoring = renderBanner({ text: "x", delta: 6, correction: false }, "Last play");
  const correction = renderBanner({ text: "x", delta: -2, correction: true }, "Last play");
  assert.ok(!scoring.includes("ffl-correction"));
  assert.ok(correction.includes("ffl-correction"));
});

test("an empty banner is still clickable", () => {
  // Before the first play of the week there is nothing to show, but the
  // history overlay should still open rather than the banner being inert.
  const html = renderBanner(null, "Last play");
  assert.ok(html.includes("No scoring plays yet"));
  assert.ok(html.includes("data-history"));
});

/* --------------------------------------------------------------- roster */

const SIDE = {
  name: "Tesla",
  points: 180.67,
  starters: [
    {
      slot: "QB",
      name: "Josh Allen",
      game: "Final W 26-7 @ Pit",
      projected: 25.38,
      points: 20.47,
      stat_line: "1 Rush TD, 123 Pass Yds",
      game_state: "post",
    },
  ],
  bench: [
    { slot: "BN", name: "Tony Pollard", game: "Final", projected: 10.5, points: 5.3, stat_line: "", game_state: "post" },
  ],
};

test("a roster shows the stat line and both numbers", () => {
  const html = renderRosterSide(SIDE);
  assert.ok(html.includes("Josh Allen"));
  assert.ok(html.includes("1 Rush TD, 123 Pass Yds"), "the per-player stat line is the web tier's advantage");
  assert.ok(html.includes("25.38"));
  assert.ok(html.includes("20.47"));
});

test("the bench is collapsed by default", () => {
  const html = renderRosterSide(SIDE);
  assert.ok(html.includes("<details"), "bench must not push starters off screen");
  assert.ok(!html.includes("<details open"));
  assert.ok(html.includes("Bench (1)"));
});

test("game state drives a per-row class", () => {
  const live = renderRosterSide({ ...SIDE, starters: [{ ...SIDE.starters[0], game_state: "in" }] });
  assert.ok(live.includes("ffl-p-live"));
  const pre = renderRosterSide({ ...SIDE, starters: [{ ...SIDE.starters[0], game_state: "unknown" }] });
  assert.ok(pre.includes("ffl-p-pre"));
});

/* -------------------------------------------------------------- history */

test("history renders newest-first as given", () => {
  const html = renderHistory([
    { player: "A", text: "first", delta: 6, correction: false },
    { player: "B", text: "second", delta: 3, correction: false },
  ]);
  assert.ok(html.indexOf("first") < html.indexOf("second"), "order must be preserved");
});

test("an empty history says so rather than rendering an empty list", () => {
  assert.ok(renderHistory([]).includes("No scoring plays"));
});

test("corrections are visually distinct in the history", () => {
  const html = renderHistory([{ player: "A", text: "x", delta: -1.2, correction: true }]);
  assert.ok(html.includes("ffl-correction"));
  assert.ok(html.includes("-1.20"));
});
