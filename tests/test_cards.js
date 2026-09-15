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

const {
  CARD_CSS,
  escapeHtml,
  fmtPoints,
  fmtDelta,
  renderMatchupRow,
  renderHeader,
  renderRowPlay,
  renderWinBar,
  trendClass,
  renderLineup,
  renderRosters,
  renderPlayerBlock,
  renderHistory,
  renderNflGame,
  renderNflField,
  renderNflPlays,
  fmtKickoff,
  findNflGamesEntity,
  renderFinalsToggle,
  FflNflGamesCard,
} = cards;

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
  const html = renderRowPlay({ text: `<img onerror=alert(1)>`, delta: 6, correction: false }, "w1.m1");
  assert.ok(!html.includes("<img"), "raw img tag leaked into the play line");
});

test("a hostile player name cannot inject markup", () => {
  const html = renderPlayerBlock(
    { slot: "QB", name: `<b>x</b>`, game: "", projected: 1, points: 1, stat_line: "", game_state: "post" },
    "home"
  );
  assert.ok(!html.includes("<b>x</b>"));
});

/* ------------------------------------------------------------ score rows */

test("a collapsed row shows names and scores, and nothing else", () => {
  const html = renderMatchupRow(ROW);
  for (const expected of ["Tesla", "Your daddy", "180.67", "104.09"]) {
    assert.ok(html.includes(expected), `missing ${expected}`);
  }
  // The projections cost two extra lines per matchup to carry numbers that
  // barely move between polls. They belong to the expanded panel.
  assert.ok(!html.includes("138.49"), "the home projection must not be on the row");
  assert.ok(!html.includes("126.61"), "the away projection must not be on the row");
  assert.ok(!html.includes("ffl-projs"), "no projection block on a collapsed row");
});

test("the collapsed row shows both live projections, coloured", () => {
  const html = renderMatchupRow({
    matchup_id: "w1.m1", index: 1,
    home: { team_id: "1", name: "Tesla", points: 76.14, projected: 122.73, live_projected: 133.93 },
    away: { team_id: "10", name: "Herb", points: 107.1, projected: 130.66, live_projected: 121.4 },
    leader: "10", last_play: null, win_prob: 0.25,
  });
  assert.ok(html.includes("133.93"), "home live projection on the row");
  assert.ok(html.includes("121.40"), "away live projection on the row");
  // Above its pre-game number is green, below is red.
  assert.ok(html.includes("ffl-rowproj ffl-up"));
  assert.ok(html.includes("ffl-rowproj ffl-down"));
});

test("the projections share the play line's row, not one of their own", () => {
  // A row of their own cost every matchup a third line.
  const html = renderMatchupRow({
    matchup_id: "w1.m1", index: 1,
    home: { team_id: "1", name: "Tesla", points: 76.14, projected: 122.73, live_projected: 133.93 },
    away: { team_id: "10", name: "Herb", points: 107.1, projected: 130.66, live_projected: 121.4 },
    leader: "10", win_prob: 0.25,
    last_play: { text: "x", short_text: "J. Taylor 1 rush, 13 yds", delta: 1.3, side: "away" },
  });
  const foot = html.slice(html.indexOf("ffl-row-foot"));
  assert.ok(foot.includes("ffl-rowprojs"), "projections belong to the foot");
  assert.ok(foot.includes("J. Taylor"), "and so does the play");
  assert.ok(!html.slice(html.indexOf("ffl-scores"), html.indexOf("ffl-row-foot")).includes("ffl-rowproj"));
});

test("a level projection is grey rather than green or red", () => {
  const html = renderMatchupRow({
    matchup_id: "w1.m1", index: 1,
    home: { team_id: "1", name: "A", points: 0, projected: 130.0, live_projected: 130.0 },
    away: { team_id: "2", name: "B", points: 0, projected: 120.0, live_projected: 120.0 },
    leader: null, last_play: null, win_prob: 0.5,
  });
  assert.ok(html.includes("ffl-rowproj ffl-flat"));
  assert.ok(!html.includes("ffl-up") && !html.includes("ffl-down"));
});

test("a row with no projection at all shows no projection line", () => {
  const html = renderMatchupRow({
    matchup_id: "w1.m1", index: 1,
    home: { team_id: "1", name: "A", points: 10 },
    away: { team_id: "2", name: "B", points: 12 },
    leader: "2", last_play: null,
  });
  assert.ok(!html.includes("ffl-rowproj"));
});

test("expanding a row reveals both projections", () => {
  const html = renderMatchupRow(ROW, { expanded: true, detailHtml: "<div></div>" });
  assert.ok(html.includes("ffl-projs"));
  for (const expected of ["138.49", "126.61"]) {
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

/* ------------------------------------------------------------- play line */

test("the play line shows the play text but not its delta", () => {
  // The delta lives on the expanded play list only; on the row it was the
  // part that forced a phone-width line to truncate.
  const html = renderRowPlay({ text: "Nacua 24 Yd TD", delta: 6.4, side: "home" }, "w1.m1");
  assert.ok(html.includes("Nacua 24 Yd TD"));
  assert.ok(!html.includes("+6.40"));
  assert.ok(!html.includes("play-delta"));
  assert.ok(html.includes("data-history"), "the play line must open the history overlay");
  assert.ok(html.includes('data-matchup-id="w1.m1"'), "history must be scoped to this matchup");
});

test("the row shows the compact player form, not the full sentence", () => {
  // On a matchup row the subject is the manager's player. The full sentence
  // leads with a quarterback who may be on nobody's roster.
  const html = renderRowPlay(
    {
      text: "Tyler Shough passed to Travis Etienne Jr. to the right for 1 yard gain",
      short_text: "T. Etienne Jr. 1 rec, 1 yd",
      delta: 1.1,
      side: "home",
    },
    "w1.m1"
  );
  assert.ok(html.includes("T. Etienne Jr. 1 rec, 1 yd"));
  assert.ok(!html.includes("Tyler Shough"));
});

test("the row falls back to the long text when there is no short form", () => {
  // A payload from an older integration build must still render.
  const html = renderRowPlay({ text: "Nacua 24 Yd TD", delta: 6.4, side: "home" }, "w1.m1");
  assert.ok(html.includes("Nacua 24 Yd TD"));
});

test("the expanded history keeps Yahoo's full sentence", () => {
  const html = renderHistory([
    {
      event_id: "e1",
      player: "Travis Etienne Jr.",
      text: "Tyler Shough passed to Travis Etienne Jr. to the right for 1 yard gain",
      short_text: "T. Etienne Jr. 1 rec, 1 yd",
      delta: 1.1,
    },
  ]);
  assert.ok(html.includes("Tyler Shough passed to Travis Etienne Jr."));
});

// The declaration block of one CSS rule, by its selector. A `{` on the
// selector's own line is what keeps `.ffl-row-play` from matching
// `.ffl-row-play-text`.
const rule = (selector) => {
  const at = CARD_CSS.indexOf(selector + " {");
  assert.ok(at !== -1, `${selector} rule not found`);
  return CARD_CSS.slice(at, CARD_CSS.indexOf("}", at));
};

test("both foot children are pinned to one grid row", () => {
  // Grid packs sparsely: an AWAY play takes column 3, pushing the cursor past
  // column 2, so a sibling with only a column set wraps to a second row. That
  // made every away-scoring matchup a line taller and read as a data problem.
  assert.match(rule(".ffl-row-play"), /grid-row:\s*1/);
  assert.match(rule(".ffl-rowprojs"), /grid-row:\s*1/);
});

test("the play sits on the scoring side's own track", () => {
  const away = renderRowPlay({ text: "x", short_text: "x", delta: 1, side: "away" }, "w1.m1");
  const home = renderRowPlay({ text: "x", short_text: "x", delta: 1, side: "home" }, "w1.m1");
  assert.ok(away.includes("ffl-play-away"));
  assert.ok(home.includes("ffl-play-home"));
});

test("the play aligns to the side of the team that scored it", () => {
  const home = renderRowPlay({ text: "T", delta: 6, side: "home" }, "m");
  const away = renderRowPlay({ text: "T", delta: 6, side: "away" }, "m");
  assert.ok(home.includes("ffl-play-home"));
  assert.ok(away.includes("ffl-play-away"));
  // Alignment is the CSS's job — text-align follows the side, like the names.
  assert.match(rule(".ffl-play-home"), /text-align:\s*start/);
  assert.match(rule(".ffl-play-away"), /text-align:\s*end/);
});

test("the row play text wraps instead of truncating", () => {
  assert.match(rule(".ffl-row-play-text"), /white-space:\s*normal/);
  assert.doesNotMatch(rule(".ffl-row-play-text"), /text-overflow/);
});

test("the projections hold their place under the scores however long the play is", () => {
  // Centre alignment let a wrapped play drag the projections down with it.
  assert.match(rule(".ffl-row-foot"), /align-items:\s*baseline/);
});

test("the live badge is centred on the card, whatever the title's width", () => {
  // A 3-track grid with each part naming its column: the badge is the
  // middle track on the league card and the NFL card alike, so the two line
  // up when stacked.
  assert.match(rule(".ffl-header"), /grid-template-columns:\s*minmax\(0, 1fr\) auto minmax\(0, 1fr\)/);
  assert.match(rule(".ffl-header-name"), /grid-column:\s*1/);
  assert.match(rule(".ffl-header-live"), /grid-column:\s*2/);
  assert.match(rule(".ffl-header-week"), /grid-column:\s*3/);
});

test("a player's stat line wraps rather than truncating in the lineup", () => {
  assert.match(rule(".ffl-lu-stat"), /white-space:\s*normal/);
  assert.doesNotMatch(rule(".ffl-lu-stat"), /text-overflow/);
});

test("a narrow row breaks between the player and the result, not inside either", () => {
  const html = renderRowPlay(
    { text: "x", short_text: "A. St. Brown 1 rec, 23 yds, 1 TD", short_who: "A. St. Brown", short_what: "1 rec, 23 yds, 1 TD", side: "home" },
    "m"
  );
  assert.match(html, /<span class="ffl-row-play-who">A\. St\. Brown<\/span> <span class="ffl-row-play-what">1 rec, 23 yds, 1 TD<\/span>/);
  // Atomic to the line breaker, and the name itself never splits.
  assert.match(rule(".ffl-row-play-who"), /display:\s*inline-block/);
  assert.match(rule(".ffl-row-play-who"), /white-space:\s*nowrap/);
  assert.match(rule(".ffl-row-play-what"), /display:\s*inline-block/);
});

test("a payload without the halves still renders as one run of text", () => {
  const html = renderRowPlay({ text: "x", short_text: "T. Etienne Jr. 1 rec, 1 yd", side: "home" }, "m");
  assert.ok(html.includes("T. Etienne Jr. 1 rec, 1 yd"));
  assert.ok(!html.includes("ffl-row-play-who"));
});

test("a play whose side cannot be resolved still renders", () => {
  const html = renderRowPlay({ text: "T", delta: 6 }, "m");
  assert.ok(html.includes("ffl-play-unknown"));
});

test("a correction is styled differently from a score", () => {
  const scoring = renderRowPlay({ text: "x", delta: 6, correction: false }, "m");
  const correction = renderRowPlay({ text: "x", delta: -2, correction: true }, "m");
  assert.ok(!scoring.includes("ffl-correction"));
  assert.ok(correction.includes("ffl-correction"));
});

test("no play means no play line at all", () => {
  assert.equal(renderRowPlay(null, "m"), "");
});

/* -------------------------------------------------------------- lineup */

const P = (over) => ({
  slot: "QB",
  name: "Josh Allen",
  nfl_team: "Buf",
  game: "Final W 26-7 @ Pit",
  projected: 25.38,
  live_projected: 20.47,
  points: 20.47,
  stat_line: "1 Rush TD, 123 Pass Yds",
  game_state: "post",
  ...over,
});

const SIDES = [
  { starters: [P({}), P({ slot: "WR", name: "Puka Nacua" })], bench: [P({ slot: "BN", name: "Tony Pollard" })] },
  { starters: [P({ name: "J. Herbert" }), P({ slot: "WR", name: "N. Collins" })], bench: [] },
];

test("both teams' players appear, facing each other", () => {
  const html = renderLineup(SIDES);
  assert.ok(html.includes("Josh Allen") && html.includes("J. Herbert"));
  assert.ok(html.includes("Puka Nacua") && html.includes("N. Collins"));
  assert.ok(html.includes("ffl-lu-home") && html.includes("ffl-lu-away"));
});

test("one shared slot column sits between the two sides", () => {
  const html = renderLineup(SIDES);
  // Two slots for two pairs of players, not one per player per side.
  assert.equal((html.match(/ffl-lu-slot/g) || []).length, 2);
  const first = html.indexOf("ffl-lu-slot");
  assert.ok(html.indexOf("ffl-lu-home") < first, "home comes before the slot");
  assert.ok(html.indexOf("ffl-lu-away") > first, "away comes after it");
});

test("a slot with no opponent still holds its row open", () => {
  // Otherwise every row below it shifts and the two sides stop lining up.
  const html = renderLineup([{ starters: [P({})] }, { starters: [] }]);
  assert.ok(html.includes("ffl-lu-empty"));
  assert.equal((html.match(/ffl-lu-slot/g) || []).length, 1);
});

test("slots are colour-coded by position", () => {
  assert.ok(renderLineup(SIDES).includes("ffl-slot-qb"));
  assert.ok(renderLineup(SIDES).includes("ffl-slot-wr"));
  assert.ok(renderLineup([{ starters: [P({ slot: "W/R/T" })] }, {}]).includes("ffl-slot-flex"));
});

test("a player block shows points, position, club, projection and stat line", () => {
  const html = renderPlayerBlock(P({}), "home");
  assert.ok(html.includes("Josh Allen"));
  assert.ok(html.includes("20.47"), "actual points");
  assert.ok(html.includes("QB · Buf"));
  assert.ok(html.includes("1 Rush TD, 123 Pass Yds"));
  assert.ok(html.includes("Final W 26-7 @ Pit"));
});

test("a player block shows the live projection, coloured, once it has moved", () => {
  const down = renderPlayerBlock(P({ projected: 25.38, live_projected: 20.47 }), "home");
  assert.match(down, /ffl-lu-proj ffl-down/);
  assert.ok(down.includes("20.47"));
  const flat = renderPlayerBlock(P({ projected: 25.38, live_projected: 25.38 }), "home");
  assert.ok(flat.includes("25.38") && !flat.includes("ffl-down") && !flat.includes("ffl-up"));
});

test("the bench is collapsed by default", () => {
  const html = renderRosters(SIDES);
  assert.ok(html.includes("<details"), "bench must not push starters off screen");
  assert.ok(!html.includes("<details open"));
  assert.ok(html.includes("Bench (1)"));
});

test("game state drives a per-block class", () => {
  assert.ok(renderPlayerBlock(P({ game_state: "in" }), "home").includes("ffl-p-live"));
  assert.ok(renderPlayerBlock(P({ game_state: "unknown" }), "home").includes("ffl-p-pre"));
  assert.ok(renderPlayerBlock(P({ game_state: "post" }), "home").includes("ffl-p-final"));
});

test("an empty matchup says so rather than rendering a bare grid", () => {
  assert.match(renderRosters([]), /No roster available/);
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

// ---------------------------------------------------------------------------
// Per-matchup last play — the GameChannel rail, five games at once
// ---------------------------------------------------------------------------

test("each matchup row carries its own last play", () => {
  const html = renderMatchupRow({
    matchup_id: "w1.m4",
    index: 4,
    home: { team_id: "5", name: "Your daddy", points: 13.96, projected: 129.61 },
    away: { team_id: "7", name: "Show me your TDs", points: 12.0, projected: 138.5 },
    leader: "5",
    last_play: { event_id: "e1", text: "D. Maye 1 Comp, 13 Pass Yds", delta: 0.49, correction: false },
  });
  assert.match(html, /D\. Maye 1 Comp, 13 Pass Yds/);
  assert.match(html, /ffl-row-play/);
});

test("a matchup with no play yet renders no play line at all", () => {
  const html = renderMatchupRow({
    matchup_id: "w1.m5",
    index: 5,
    home: { team_id: "6", name: "The Nation", points: 0, projected: 140.78 },
    away: { team_id: "9", name: "Lightning Bolts", points: 0, projected: 145.75 },
    leader: null,
    last_play: null,
  });
  assert.ok(!html.includes("ffl-row-play"), "an empty strip would waste a row of height");
});

test("the play line is a sibling of the row, never nested inside it", () => {
  // Two buttons — the row (rosters) and the play line (history) — but the
  // second must start after the first one's element has closed, or the nested
  // roles break keyboard navigation and screen-reader labelling.
  const html = renderMatchupRow({
    matchup_id: "w1.m1",
    index: 1,
    home: { team_id: "1", name: "A", points: 1, projected: 2 },
    away: { team_id: "2", name: "B", points: 3, projected: 4 },
    leader: "2",
    last_play: { event_id: "e", text: "x", delta: 1, correction: false, side: "away" },
  });
  assert.equal((html.match(/role="button"/g) || []).length, 2);
  assert.ok(html.indexOf("ffl-row-play") > html.indexOf("ffl-team-end"));
});

test("a play line escapes its text", () => {
  const html = renderMatchupRow({
    matchup_id: "w1.m1",
    index: 1,
    home: { team_id: "1", name: "A", points: 1, projected: 2 },
    away: { team_id: "2", name: "B", points: 3, projected: 4 },
    leader: "1",
    last_play: { event_id: "e", text: "<img src=x onerror=alert(1)>", delta: 1, correction: false },
  });
  assert.ok(!html.includes("<img"), "the play text reaches innerHTML, so it must be escaped");
});

test("a correction marks the row's play line", () => {
  const html = renderMatchupRow({
    matchup_id: "w1.m2",
    index: 2,
    home: { team_id: "2", name: "Wolfpack", points: 6.7, projected: 133.99 },
    away: { team_id: "4", name: "PAPER CHAMP", points: 0, projected: 135.27 },
    leader: "2",
    last_play: { event_id: "e", text: "R. Stevenson -0.10 (stat correction)", delta: -0.1, correction: true },
  });
  assert.match(html, /ffl-row-play ffl-play-\w+ ffl-correction/);
});

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

const ST = (attrs) => ({ attributes: attrs });

test("the header shows the league's own name and week", () => {
  const html = renderHeader(ST({ league_name: "Kush", week: 1 }), {});
  assert.match(html, /Kush/);
  assert.match(html, /Week 1/);
});

test("an explicit card title beats the league name", () => {
  const html = renderHeader(ST({ league_name: "Kush", week: 1 }), { title: "My League" });
  assert.match(html, /My League/);
  assert.ok(!html.includes("Kush"));
});

test("the header escapes a hostile league name", () => {
  const html = renderHeader(ST({ league_name: "<img src=x onerror=alert(1)>", week: 2 }), {});
  assert.ok(!html.includes("<img"));
});

test("no name and no week renders no header at all", () => {
  assert.equal(renderHeader(ST({}), {}), "");
});

test("a league with a name but no week still gets a header", () => {
  const html = renderHeader(ST({ league_name: "Kush" }), {});
  assert.match(html, /Kush/);
  assert.ok(!html.includes("Week"));
});

// ---------------------------------------------------------------------------
// Inline accordion
// ---------------------------------------------------------------------------

const ROW_A = {
  matchup_id: "w1.m3",
  index: 3,
  home: { team_id: "3", name: "imgonna git u sucka", points: 24.1, projected: 149.94 },
  away: { team_id: "8", name: "Wallyworld 34", points: 0, projected: 136.76 },
  leader: "3",
  last_play: null,
};

test("a collapsed row carries no detail panel", () => {
  const html = renderMatchupRow(ROW_A);
  assert.match(html, /aria-expanded="false"/);
  assert.ok(!html.includes("ffl-row-detail"));
});

test("an expanded row renders its detail beneath itself", () => {
  const html = renderMatchupRow(ROW_A, { expanded: true, detailHtml: "<p>ROSTERS</p>" });
  assert.match(html, /aria-expanded="true"/);
  assert.match(html, /ffl-row-detail/);
  assert.match(html, /ROSTERS/);
  // The panel must come after the row it belongs to, not before it.
  assert.ok(html.indexOf("ffl-row-detail") > html.indexOf('data-matchup-index="3"'));
});

test("an expanded row with no payload yet shows a loading state", () => {
  const html = renderMatchupRow(ROW_A, { expanded: true });
  assert.match(html, /Loading rosters/);
});

test("aria-controls points at the panel's real id", () => {
  const html = renderMatchupRow(ROW_A, { expanded: true, detailHtml: "x" });
  const controls = /aria-controls="([^"]+)"/.exec(html)[1];
  assert.match(html, new RegExp(`id="${controls}"`));
  // Ids must be valid HTML ids, and "w1.m3" contains a dot.
  assert.ok(!controls.includes("."), controls);
});

test("there is no chevron; the row itself is the control", () => {
  const html = renderMatchupRow(ROW_A);
  assert.ok(!html.includes("ffl-chevron"), "the arrow is gone");
  assert.ok(!html.includes("ffl-row-toggle"));
  // The click handler resolves via closest("[data-matchup-index]"), so the row
  // carrying it is what keeps the whole card clickable without the arrow.
  assert.ok(html.includes('data-matchup-index="3"'));
  assert.ok(html.includes('role="button"'));
  assert.ok(html.includes('aria-expanded="false"'));
});

test("the foot is dropped only when it would be empty", () => {
  // No play but a projection still needs the row.
  const withProj = renderMatchupRow({
    matchup_id: "w1.m1", index: 1,
    home: { team_id: "1", name: "A", points: 1, projected: 120, live_projected: 130 },
    away: { team_id: "2", name: "B", points: 2, projected: 120, live_projected: 110 },
    leader: "2", last_play: null,
  });
  assert.ok(withProj.includes("ffl-row-foot"));

  // Neither: an empty padded strip is wasted height.
  const bare = renderMatchupRow({
    matchup_id: "w1.m1", index: 1,
    home: { team_id: "1", name: "A", points: 1 },
    away: { team_id: "2", name: "B", points: 2 },
    leader: "2", last_play: null,
  });
  assert.ok(!bare.includes("ffl-row-foot"));
});

test("the expanded wrapper is marked so the panel can be styled", () => {
  assert.match(renderMatchupRow(ROW_A, { expanded: true }), /ffl-row-wrap ffl-expanded/);
  assert.ok(!renderMatchupRow(ROW_A).includes("ffl-expanded"));
});

// ---------------------------------------------------------------------------
// Live projections
// ---------------------------------------------------------------------------

test("a projection above the pre-game number is green, below is red, level is grey", () => {
  assert.equal(trendClass(21.46, 19.97), " ffl-up");
  assert.equal(trendClass(123.15, 135.35), " ffl-down");
  assert.equal(trendClass(19.97, 19.97), " ffl-flat");
  assert.equal(trendClass(null, 19.97), "");
});

test("an expanded team shows the original projection and the live one", () => {
  const html = renderMatchupRow({
    matchup_id: "w1.m1",
    index: 1,
    home: { team_id: "1", name: "Tesla", points: 12.1, projected: 135.35, live_projected: 123.15 },
    away: { team_id: "10", name: "Pass the Herb", points: 28.4, projected: 130.46, live_projected: 136.11 },
    leader: "10",
    last_play: null,
    win_prob: 0.39,
  }, { expanded: true, detailHtml: "<div></div>" });
  assert.ok(html.includes("orig 135.35"), "the pre-game number keeps its place");
  assert.ok(html.includes("proj 123.15"));
  assert.ok(html.includes("ffl-team-live ffl-down"), "Tesla is below its projection");
  assert.ok(html.includes("ffl-team-live ffl-up"), "the opponent is above its own");
});

test("before kickoff there is only one projection to show", () => {
  const html = renderMatchupRow({
    matchup_id: "w1.m1",
    index: 1,
    home: { team_id: "1", name: "A", points: 0, projected: 135.35, live_projected: 135.35 },
    away: { team_id: "2", name: "B", points: 0, projected: 130.46, live_projected: 130.46 },
    leader: null,
    last_play: null,
    win_prob: 0.52,
  }, { expanded: true, detailHtml: "<div></div>" });
  assert.ok(html.includes("proj 135.35"));
  assert.ok(!html.includes("orig"), "an identical second line is noise");
});

test("the win bar marks the favourite and only renders when expanded", () => {
  const row = {
    matchup_id: "w1.m1",
    index: 1,
    home: { team_id: "1", name: "Tesla", points: 12.1, projected: 135.35, live_projected: 123.15 },
    away: { team_id: "10", name: "Pass the Herb", points: 28.4, projected: 130.46, live_projected: 136.11 },
    leader: "10",
    last_play: null,
    win_prob: 0.39,
  };
  assert.equal(renderWinBar(row).includes("39%"), true);
  assert.ok(renderWinBar(row).includes("61%"));
  assert.match(renderWinBar(row), /Pass the Herb projected to win, 61%/);
  assert.ok(!renderMatchupRow(row).includes("ffl-winbar"), "collapsed rows stay compact");
  assert.ok(renderMatchupRow(row, { expanded: true }).includes("ffl-winbar"));
});

test("an unknowable win probability renders nothing at all", () => {
  assert.equal(renderWinBar({ win_prob: null, home: {}, away: {} }), "");
});

test("a player over their projection reads green", () => {
  const html = renderPlayerBlock(
    {
      slot: "WR",
      name: "P. Nacua",
      nfl_team: "LAR",
      game: "2nd 14:57 7-3 vs SF",
      projected: 19.97,
      live_projected: 21.46,
      points: 6.5,
      stat_line: "2 Rec, 45 Rec Yds",
      game_state: "in",
    },
    "home"
  );
  assert.match(html, /ffl-lu-proj ffl-up/);
  assert.ok(html.includes("21.46"));
});

test("possession and the red zone are flagged on the player who has the ball", () => {
  const ball = renderPlayerBlock(P({ has_ball: true }), "home");
  assert.match(ball, /ffl-ball/);
  assert.ok(!ball.includes("ffl-rz"), "having the ball is not being in the red zone");

  const rz = renderPlayerBlock(P({ has_ball: true, red_zone: true }), "home");
  assert.match(rz, /ffl-rz/);
  assert.ok(rz.includes(">RZ<"));

  const neither = renderPlayerBlock(P({}), "home");
  assert.ok(!neither.includes("ffl-ball") && !neither.includes("ffl-rz"));
});

test("an injury designation rides beside the name", () => {
  assert.match(renderPlayerBlock(P({ status: "Q" }), "home"), /ffl-status ffl-status-q/);
  assert.ok(!renderPlayerBlock(P({ status: "" }), "home").includes("ffl-status"));
});

// ---------------------------------------------------------------------------
// Two panels, one at a time
// ---------------------------------------------------------------------------

const PANEL_ROW = {
  matchup_id: "w1.m1",
  index: 1,
  home: { team_id: "1", name: "Tesla", points: 12.1, projected: 135.35, live_projected: 123.15 },
  away: { team_id: "2", name: "Herb", points: 28.4, projected: 130.46, live_projected: 136.11 },
  leader: "2",
  win_prob: 0.39,
  last_play: { event_id: "e", text: "P. Nacua 1 Rec, 6 Rec Yds", delta: 1.6, side: "home" },
};

test("the play list expands inline, not in a dialog", () => {
  const html = renderMatchupRow(PANEL_ROW, { expanded: true, panel: "plays", detailHtml: "<p>PLAYS</p>" });
  assert.match(html, /ffl-row-detail/);
  assert.match(html, /PLAYS/);
  assert.ok(html.indexOf("PLAYS") > html.indexOf("ffl-row-play"), "the panel follows the line that opens it");
});

test("the play line reports its own expanded state", () => {
  const open = renderMatchupRow(PANEL_ROW, { expanded: true, panel: "plays" });
  assert.match(open, /ffl-play-open/);
  assert.match(open, /aria-expanded="true"/);
  const shut = renderMatchupRow(PANEL_ROW);
  assert.ok(!shut.includes("ffl-play-open"));
});

test("the row does not claim to be open when the play list is", () => {
  // The row's aria-expanded tracks the LINEUP panel; only the play line
  // should report itself open when the play list is what is showing.
  const plays = renderMatchupRow(PANEL_ROW, { expanded: true, panel: "plays" });
  assert.match(plays, /ffl-plays-open/);
  assert.equal((plays.match(/aria-expanded="true"/g) || []).length, 1, "only the play line");

  const roster = renderMatchupRow(PANEL_ROW, { expanded: true, panel: "roster" });
  assert.ok(!roster.includes("ffl-plays-open"));
});

test("the win bar belongs to the lineup, not the play list", () => {
  assert.ok(renderMatchupRow(PANEL_ROW, { expanded: true, panel: "roster" }).includes("ffl-winbar"));
  assert.ok(!renderMatchupRow(PANEL_ROW, { expanded: true, panel: "plays" }).includes("ffl-winbar"));
});

test("each panel says what it is loading", () => {
  assert.match(renderMatchupRow(PANEL_ROW, { expanded: true, panel: "plays" }), /Loading plays/);
  assert.match(renderMatchupRow(PANEL_ROW, { expanded: true, panel: "roster" }), /Loading rosters/);
});

/* -------------------------------------------------------------- NFL games */

const GAME_LIVE = {
  game_id: "20260913016", plays_id: "16", state: "in",
  clock_text: "Q3 6:24", situation: "2nd & 7", ball_on: "Det 20", yards_to_goal: 20, start_time: 1789000000,
  last_play: "Alvin Kamara rushed to the left for 4 yard gain",
  away: { team_id: "9", abbr: "NO", score: 17, has_ball: true, red_zone: true },
  home: { team_id: "16", abbr: "Det", score: 24, has_ball: false, red_zone: false },
};

test("a live game shows score, clock, situation and the ball spot", () => {
  const html = renderNflGame(GAME_LIVE);
  for (const bit of ["NO", "Det", "17", "24", "Q3 6:24"]) {
    assert.ok(html.includes(bit), `missing ${bit}`);
  }
  // One phrase, the way a broadcast says it — not two adjacent facts.
  assert.ok(html.includes("2nd &amp; 7, Det 20"), "down-and-distance reads with the spot");
});

test("the last play is always shown, not only when expanded", () => {
  const html = renderNflGame(GAME_LIVE);
  assert.ok(html.includes("ffl-nfl-last"));
  assert.ok(html.includes("Alvin Kamara rushed to the left for 4 yard gain"));
  assert.ok(!html.includes("ffl-nfl-plays"), "and without opening the panel");
});

test("a game with no last play yet renders no empty line for it", () => {
  const html = renderNflGame({ ...GAME_LIVE, last_play: "" });
  assert.ok(!html.includes("ffl-nfl-last"));
});

test("the ball spot alone still reads when there is no down and distance", () => {
  const html = renderNflGame({ ...GAME_LIVE, situation: "", ball_on: "50" });
  assert.ok(html.includes(">50<"));
});

test("there is no game progress bar", () => {
  const html = renderNflGame(GAME_LIVE);
  assert.ok(!html.includes("ffl-nfl-bar"));
});

/** The two team blocks, away first — slicing on the abbreviation alone would
 *  match the aria-label, which names both sides before either block. */
function teamBlocks(html) {
  const teams = html.slice(html.indexOf("ffl-nfl-teams"), html.indexOf("ffl-nfl-status"));
  const parts = teams.split('<div class="ffl-nfl-team');
  assert.equal(parts.length, 3, "expected exactly two team blocks");
  return { away: parts[1], home: parts[2] };
}

test("possession and red zone attach to the team, not the game", () => {
  const { away, home } = teamBlocks(renderNflGame(GAME_LIVE));
  assert.ok(away.includes("ffl-poss"), "the ball belongs to the side holding it");
  assert.ok(away.includes("ffl-rz"));
  assert.ok(!home.includes("ffl-poss"), "the side without the ball gets no glyph");
  assert.ok(!home.includes("ffl-rz"));
});

test("the leading side is the one marked", () => {
  const html = renderNflGame(GAME_LIVE);
  const { away, home } = teamBlocks(html);
  // Detroit leads 24-17.
  assert.ok(home.includes("ffl-nfl-lead"));
  assert.ok(!away.includes("ffl-nfl-lead"));
  assert.equal((html.match(/ffl-nfl-lead/g) || []).length, 1, "exactly one leader");
});

test("a tied game marks neither side as leading", () => {
  const tied = { ...GAME_LIVE, away: { ...GAME_LIVE.away, score: 21 }, home: { ...GAME_LIVE.home, score: 21 } };
  assert.ok(!renderNflGame(tied).includes("ffl-nfl-lead"));
});

test("a game that has not kicked off shows its start time, never a blank", () => {
  const html = renderNflGame({ ...GAME_LIVE, state: "pre", clock_text: "", situation: "", elapsed: 0 });
  const status = html.slice(html.indexOf("ffl-nfl-status"));
  assert.ok(!/ffl-nfl-clock[^>]*>\s*</.test(status), "the clock slot must say something");
});

test("an unparseable kickoff falls back rather than printing Invalid Date", () => {
  assert.equal(fmtKickoff(null), "");
  assert.equal(fmtKickoff(0), "");
  assert.equal(fmtKickoff("nonsense"), "");
  assert.ok(fmtKickoff(1789000000).length > 0);
});

test("the field bar fills from the offence's own goal line to the ball", () => {
  // 20 to go means the drive has covered 80 yards: 80% filled, red inside
  // the 20 to match the RZ badge.
  const html = renderNflGame(GAME_LIVE);
  assert.ok(html.includes("ffl-nfl-field"));
  assert.ok(html.includes("width:80%"));
  assert.ok(html.includes("ffl-nfl-field-rz"));
  assert.ok(html.indexOf("ffl-nfl-field") < html.indexOf("ffl-nfl-last"), "field above the play, as Yahoo draws it");

  const midfield = renderNflField({ ...GAME_LIVE, yards_to_goal: 50 });
  assert.ok(midfield.includes("width:50%"));
  assert.ok(!midfield.includes("ffl-nfl-field-rz"));
  assert.ok(renderNflField({ ...GAME_LIVE, yards_to_goal: 81 }).includes("width:19%"));
});

test("no real ball spot means no field bar at all", () => {
  // Null is the integration's "no spot" — before kickoff, at half time,
  // between a score and the kickoff. Never an empty track.
  for (const ytg of [null, undefined, 0, 100, "", "nope"]) {
    assert.equal(renderNflField({ ...GAME_LIVE, yards_to_goal: ytg }), "", `ytg=${ytg}`);
  }
  assert.equal(renderNflField({ ...GAME_LIVE, state: "post" }), "");
  assert.equal(renderNflField({ ...GAME_LIVE, state: "pre" }), "");
});

test("a finished game says Final and carries no live situation", () => {
  const html = renderNflGame({
    ...GAME_LIVE, state: "post", clock_text: "Final", situation: "", ball_on: "", last_play: "",
  });
  assert.ok(html.includes("Final"));
  assert.ok(!html.includes("ffl-nfl-situation"));
  assert.ok(!html.includes("ffl-nfl-live"), "a final game is not live");
});

test("a game is only expanded when it is the open one", () => {
  assert.ok(!renderNflGame(GAME_LIVE).includes("ffl-nfl-plays"));
  const open = renderNflGame(GAME_LIVE, { open: true, playsHtml: "<b>x</b>" });
  assert.ok(open.includes("ffl-nfl-plays"));
  assert.ok(open.includes('aria-expanded="true"'));
});

test("the play list reads newest first and survives an empty feed", () => {
  assert.ok(renderNflPlays([]).includes("No plays yet"));
  const html = renderNflPlays([
    { play_id: "16.90", text: "Newest thing", period: "3", clock: "6:24" },
    { play_id: "16.89", text: "Older thing", period: "3", clock: "7:01" },
  ]);
  assert.ok(html.indexOf("Newest thing") < html.indexOf("Older thing"));
  assert.ok(html.includes("Q3"));
});

test("a hostile play description cannot inject markup into the games card", () => {
  const html = renderNflPlays([{ text: `<img onerror=alert(1)>`, period: "1", clock: "1:00" }]);
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes("&lt;img"));
});

test("the games entity is found by its games array, not its id", () => {
  const hass = {
    states: {
      "sensor.unrelated": { attributes: { league_id: "1", matchups: [] } },
      "sensor.anything_at_all": { attributes: { league_id: "1", games: [] } },
    },
  };
  assert.equal(findNflGamesEntity(hass), "sensor.anything_at_all");
  assert.equal(findNflGamesEntity({}), "");
});

/* ------------------------------------------------- finished games, folded */

const SLATE = [
  { ...GAME_LIVE, game_id: "live1", state: "in" },
  { ...GAME_LIVE, game_id: "done1", state: "post", clock_text: "Final" },
  { ...GAME_LIVE, game_id: "soon1", state: "pre", clock_text: "" },
  { ...GAME_LIVE, game_id: "done2", state: "post", clock_text: "Final" },
];

function nflCard() {
  const card = new FflNflGamesCard();
  card.setConfig({ entity: "sensor.x" });
  return card;
}

test("finished games sort below everything still worth watching", () => {
  const { active, done } = nflCard()._split(SLATE);
  assert.deepEqual(active.map((g) => g.game_id), ["live1", "soon1"]);
  assert.deepEqual(done.map((g) => g.game_id), ["done1", "done2"]);
});

test("the feed's kickoff order survives inside each group", () => {
  // live1 before soon1 because it kicked off earlier — no second rule needed.
  const { active } = nflCard()._split(SLATE);
  assert.equal(active[0].game_id, "live1");
});

test("finished games start hidden while anything else is showing", () => {
  const card = nflCard();
  assert.equal(card._finalsOpen(2), false);
});

test("an all-final slate opens rather than hiding every game behind a click", () => {
  const card = nflCard();
  assert.equal(card._finalsOpen(0), true, "a card showing nothing is useless");
});

test("an explicit choice beats the default in both directions", () => {
  const card = nflCard();
  card._showFinal = true;
  assert.equal(card._finalsOpen(5), true, "opened by hand, kept open");
  card._showFinal = false;
  assert.equal(card._finalsOpen(0), false, "closed by hand, kept shut on an all-final slate");
});

test("the finals toggle says how many and which way it points", () => {
  const shut = renderFinalsToggle(8, false);
  assert.ok(shut.includes("8 final"));
  assert.ok(shut.includes('aria-expanded="false"'));
  assert.ok(shut.includes("Show 8 finished games"));

  const open = renderFinalsToggle(1, true);
  assert.ok(open.includes("ffl-nfl-finals-open"));
  assert.ok(open.includes('aria-expanded="true"'));
  assert.ok(open.includes("Hide 1 finished game"), "singular reads properly");
});
