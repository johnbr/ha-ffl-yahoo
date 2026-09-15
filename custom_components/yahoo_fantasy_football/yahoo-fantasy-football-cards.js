/* Yahoo Fantasy Football cards for Home Assistant. */
const CARD_VERSION = "0.10.0"; // x-release-please-version

const MY_MATCHUP_TAG = "ffl-my-matchup-card";
const LEAGUE_TAG = "ffl-league-scoreboard-card";
const NFL_GAMES_TAG = "ffl-nfl-games-card";
const MY_MATCHUP_EDITOR = "ffl-my-matchup-card-editor";
const LEAGUE_EDITOR = "ffl-league-scoreboard-card-editor";
const NFL_GAMES_EDITOR = "ffl-nfl-games-card-editor";
const DOCS_URL = "https://github.com/johnbr/ha-ffl-yahoo";
const STYLE_CLASS = "ffl-card-style";
const DOMAIN = "yahoo_fantasy_football";

/*
 * Both cards share this one bundle so they can share the render helpers, the
 * expandable panels and the CSS — two custom elements, one Lovelace resource.
 *
 * Structural choices carried over from the MLB card, each for a concrete reason:
 *   - plain HTMLElement + innerHTML, light DOM, zero imports (no build step)
 *   - the stylesheet is injected INSIDE the card element, because Lovelace
 *     nests cards in shadow roots that a document.head sheet cannot reach
 *   - a SCALAR-ONLY render fingerprint guards the innerHTML replacement. HA
 *     pushes a new hass object on every state change of every entity, so an
 *     unguarded rebuild thrashes the DOM many times a second and destroys the
 *     browser's scroll anchoring.
 *   - click/keydown listeners are delegated and attached ONCE, because
 *     innerHTML replacement would orphan per-element handlers.
 */

/* ------------------------------------------------------------------ utils */

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]
  );
}

/** Points as shown on Yahoo: two decimals, em dash when genuinely absent. */
function fmtPoints(value) {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(2) : "—";
}

function fmtDelta(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}`;
}

/**
 * Which way a live projection has moved against the pre-game one.
 *
 * Grey is the honest answer when they are equal, which is the normal state
 * before kickoff and vanishingly rare once a game is running.
 */
function trendClass(live, projected) {
  // Number(null) is 0, which is finite — so an absent projection would read as
  // a catastrophic drop to zero rather than as "unknown".
  const num = (v) => (v === null || v === undefined || v === "" ? NaN : Number(v));
  const a = num(live);
  const b = num(projected);
  if (!Number.isFinite(a) || !Number.isFinite(b)) return "";
  if (Math.abs(a - b) < 0.005) return " ffl-flat";
  return a > b ? " ffl-up" : " ffl-down";
}

/** True when a live projection is worth printing beside the original. */
function hasLive(live, projected) {
  return trendClass(live, projected) !== "" && trendClass(live, projected) !== " ffl-flat";
}

function fmtPercent(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  return `${Math.round(n * 100)}%`;
}

function ensureCardStyles(host) {
  if (!host.querySelector(`.${STYLE_CLASS}`)) {
    const style = document.createElement("style");
    style.className = STYLE_CLASS;
    style.textContent = CARD_CSS;
    host.appendChild(style);
  }
}

/*
 * Entity ids follow the LEAGUE NAME (`sensor.kush_scoreboard`), not a fixed
 * prefix — the old `sensor.ffl_*` match never matched anything real. Identify
 * the scoreboard by the attributes only it has.
 */
function findFflEntity(hass) {
  if (!hass || !hass.states) return "";
  const match = Object.keys(hass.states).find((id) => {
    const a = hass.states[id].attributes || {};
    return id.startsWith("sensor.") && a.league_id && Array.isArray(a.matchups);
  });
  return match || "";
}

/** The NFL-games sensor: the one carrying a `games` array. */
function findNflGamesEntity(hass) {
  if (!hass || !hass.states) return "";
  const match = Object.keys(hass.states).find((id) => {
    const a = hass.states[id].attributes || {};
    return id.startsWith("sensor.") && a.league_id && Array.isArray(a.games);
  });
  return match || "";
}

/**
 * Kickoff time in the VIEWER's timezone.
 *
 * The feed sends an epoch, which is the right thing to ship — a dashboard on a
 * phone in another timezone should read local, and only the browser knows what
 * local is. Returns "" for a missing or unparseable value so the caller can
 * fall back rather than print "Invalid Date".
 */
function fmtKickoff(epoch) {
  const seconds = Number(epoch);
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  try {
    return new Date(seconds * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  } catch (err) {
    return "";
  }
}

/* -------------------------------------------------------------- websocket */

function callWS(hass, message) {
  if (!hass || typeof hass.callWS !== "function") {
    return Promise.reject(new Error("Home Assistant connection unavailable"));
  }
  return hass.callWS(message);
}

function fetchNflPlays(hass, leagueId, playsId, limit) {
  return callWS(hass, {
    type: `${DOMAIN}/nfl_plays`,
    league_id: leagueId,
    plays_id: String(playsId),
    limit: limit || 12,
  });
}

function fetchMatchupDetail(hass, leagueId, matchupIndex) {
  return callWS(hass, {
    type: `${DOMAIN}/matchup_detail`,
    league_id: String(leagueId),
    matchup_index: matchupIndex,
  });
}

function fetchPlayHistory(hass, leagueId, options = {}) {
  const message = {
    type: `${DOMAIN}/play_history`,
    league_id: String(leagueId),
    limit: options.limit || 50,
    // Same rule as the card's play line: a bench player's points never counted
    // toward the score, so they were never part of this matchup.
    starters_only: true,
  };
  if (options.matchupId) message.matchup_id = options.matchupId;
  if (options.teamKey) message.team_key = options.teamKey;
  return callWS(hass, message);
}

/* ------------------------------------------------------------- rendering */

function renderTeamSide(team, isLeader, align) {
  // Name only. The projections used to stack under it, which made every
  // collapsed row three lines tall to carry two numbers that do not change
  // between polls the way the score does. With a single child the row's
  // `align-items: center` puts the name on the same line as the score.
  const cls = `ffl-team ffl-team-${align}${isLeader ? " ffl-leader" : ""}`;
  return `
    <div class="${cls}">
      <div class="ffl-team-name">${escapeHtml(team.name)}</div>
    </div>`;
}

/**
 * Both teams' projections, for the expanded panel.
 *
 * Both numbers, the way StatTracker prints "Orig Proj" over "Proj Pts": the
 * live figure alone says where a team is heading but not whether that is
 * better or worse than the morning's expectation, which is the whole point.
 *
 * Three tracks, matching the row and the play line above it, so each side
 * lands under its own team rather than floating.
 */
function renderProjections(row) {
  const side = (team, align) => {
    const live = hasLive(team.live_projected, team.projected)
      ? `<div class="ffl-team-live${trendClass(team.live_projected, team.projected)}">proj ${fmtPoints(
          team.live_projected
        )}</div>`
      : "";
    return `
      <div class="ffl-proj-side ffl-proj-${align}">
        <div class="ffl-team-proj">${live ? "orig" : "proj"} ${fmtPoints(team.projected)}</div>
        ${live}
      </div>`;
  };
  return `
    <div class="ffl-projs">
      ${side(row.home, "start")}
      <div></div>
      ${side(row.away, "end")}
    </div>`;
}

/**
 * The play that last moved THIS matchup's score.
 *
 * Aligned to the side of the team that scored it — delta outermost, text beside
 * it — so "who gained these points" is answered by position. A fixed
 * text-left/delta-right layout made every play look like it belonged to the
 * right-hand team.
 *
 * It sits OUTSIDE the row element (a sibling, never a child), so making it its
 * own button does not nest one interactive role inside another. It is the way
 * into this matchup's play history now that the league-wide banner is gone.
 */
function renderRowPlay(play, matchupId, open) {
  if (!play) return "";
  const side = play.side === "home" || play.side === "away" ? play.side : "unknown";
  // The compact player-side form on the row — "T. Etienne Jr. 1 rec, 1 yd".
  // The full sentence is not lost, it is what the expanded play list shows;
  // here it would lead with a quarterback nobody rosters and force a wrap.
  // Falls back to `text` so a payload from an older integration still renders.
  // No point delta here — the row already shows the live projection moving,
  // and "+2.80" beside a five-word play was the part that forced the line to
  // truncate on a phone. The delta is on every row of the expanded play list.
  //
  // The player and the result are two boxes, not one run of text, so that a
  // row too narrow for both breaks BETWEEN them — "A. St. Brown" over
  // "1 rec, 23 yd, 1 TD" — instead of wherever the width runs out. A payload
  // without the halves (an older integration) still renders as one span.
  const text =
    play.short_who && play.short_what
      ? `<span class="ffl-row-play-who">${escapeHtml(play.short_who)}</span> <span class="ffl-row-play-what">${escapeHtml(
          play.short_what
        )}</span>`
      : escapeHtml(play.short_text || play.text);
  return `
    <div class="ffl-row-play ffl-play-${side}${play.correction ? " ffl-correction" : ""}${open ? " ffl-play-open" : ""}"
         role="button" tabindex="0" data-history="1"
         data-matchup-id="${escapeHtml(matchupId || "")}"
         aria-expanded="${open ? "true" : "false"}"
         aria-label="Show scoring play history">
      <span class="ffl-row-play-text">${text}</span>
    </div>`;
}

/**
 * Card header: the league's own name, and the week beside it.
 *
 * `league_name` is an attribute rather than something scraped off the entity's
 * friendly name, so a renamed entity cannot desynchronise the heading from the
 * league it is showing. An explicit `title:` in the card config wins.
 */
function renderHeader(st, config) {
  const attrs = (st && st.attributes) || {};
  const name = (config && config.title) || attrs.league_name || "";
  const week = attrs.week;
  if (!name && !week) return "";
  // Only while something is actually being played: "0 games live" is noise on
  // the six days a week when that is the answer.
  const live = Number(attrs.active_games) || 0;
  const liveBadge = live ? `<span class="ffl-header-live" title="NFL games in progress">${live} live</span>` : "";
  return `
    <div class="ffl-header">
      <div class="ffl-header-name">${escapeHtml(name || "Fantasy Football")}</div>
      ${liveBadge}
      ${week ? `<div class="ffl-header-week">Week ${escapeHtml(week)}</div>` : ""}
    </div>`;
}

/**
 * Projected winner, as a split bar between the two teams.
 *
 * The percentages are this integration's own estimate from the live
 * projections and what is still to be played — Yahoo's number comes from a
 * betting market it licenses, so it cannot be reproduced, only approximated.
 */
function renderWinBar(row) {
  const p = row.win_prob;
  if (p === null || p === undefined || !Number.isFinite(Number(p))) return "";
  const home = Math.max(0, Math.min(1, Number(p)));
  const away = 1 - home;
  const homeFav = home >= away;
  return `
    <div class="ffl-winbar" role="img"
         aria-label="${escapeHtml(
           `${homeFav ? row.home.name : row.away.name} projected to win, ${fmtPercent(Math.max(home, away))}`
         )}">
      <span class="ffl-win-pct${homeFav ? " ffl-win-fav" : ""}">${fmtPercent(home)}</span>
      <div class="ffl-win-track">
        <div class="ffl-win-seg${homeFav ? " ffl-win-fav" : ""}" style="flex-grow:${home.toFixed(4)}"></div>
        <div class="ffl-win-seg${homeFav ? "" : " ffl-win-fav"}" style="flex-grow:${away.toFixed(4)}"></div>
      </div>
      <span class="ffl-win-pct${homeFav ? "" : " ffl-win-fav"}">${fmtPercent(away)}</span>
    </div>`;
}

/**
 * Both live projections, riding the MIDDLE track of the play line's row.
 *
 * They sat on their own line under the scores, which cost every matchup a
 * third line to carry two numbers. The play line's row already had an empty
 * centre track — it is where the chevron used to be — and that column is
 * centred under the scores, so the projections read as belonging to them
 * without occupying a row of their own.
 *
 * Coloured against the PRE-GAME projection: green ahead of it, red behind,
 * grey level. `trendClass` returns "" when either number is missing, which is
 * also how a team with no projection at all renders nothing.
 */
function renderRowProjections(home, away) {
  const h = trendClass(home.live_projected, home.projected);
  const a = trendClass(away.live_projected, away.projected);
  if (!h && !a) return "";
  const cell = (team, cls) =>
    cls ? `<span class="ffl-rowproj${cls}">${fmtPoints(team.live_projected)}</span>` : `<span></span>`;
  return `<div class="ffl-rowprojs">${cell(home, h)}${cell(away, a)}</div>`;
}

/**
 * The play line and the projections, sharing one row.
 *
 * Rendered only when there is something to put in it — a matchup with neither
 * a scoring play nor a projection would otherwise get an empty padded strip.
 */
function renderRowFoot(row, playsOpen) {
  const play = row.last_play ? renderRowPlay(row.last_play, row.matchup_id, playsOpen) : "";
  const projections = renderRowProjections(row.home, row.away);
  if (!play && !projections) return "";
  return `<div class="ffl-row-foot">${play}${projections}</div>`;
}

/* ------------------------------------------------------------- NFL games */

/**
 * One club's line inside a game: abbreviation, possession, red zone, score.
 *
 * The possession glyph and the RZ badge sit with the TEAM rather than in the
 * game's status column, because "who has the ball" is a fact about a side and
 * putting it anywhere else makes the reader work out which one it means.
 */
function renderNflTeam(side, isLeader) {
  const ball = side.has_ball ? `<span class="ffl-poss" aria-label="has the ball">🏈</span>` : "";
  const rz = side.red_zone ? `<span class="ffl-rz" aria-label="in the red zone">RZ</span>` : "";
  const score = side.score === null || side.score === undefined ? "" : String(side.score);
  return `
    <div class="ffl-nfl-team${isLeader ? " ffl-nfl-lead" : ""}">
      <span class="ffl-nfl-abbr">${escapeHtml(side.abbr || side.team_id || "")}</span>
      ${ball}${rz}
      <span class="ffl-nfl-score">${escapeHtml(score)}</span>
    </div>`;
}

/**
 * The field, as one bar: how far the current drive has come.
 *
 * The offence always drives LEFT TO RIGHT, whichever club it is — the fill
 * starts at their own goal line and ends where the ball is, so a long fill
 * means they are close to scoring, for every game, without a legend. Yahoo's
 * own rail draws it this way. The alternative, fixed ends per club, needs
 * the reader to know which end is whose, and nothing on the card says so.
 *
 * `yards_to_goal` is null whenever the integration has no real spot — before
 * kickoff, at half time, between a score and the kickoff — and the bar is
 * simply absent then rather than drawn empty; the caption beside it is gone
 * for the same reason, by the same gate.
 */
function renderNflField(game) {
  if (game.state !== "in") return "";
  const toGoal = Number(game.yards_to_goal);
  if (!Number.isFinite(toGoal) || toGoal <= 0 || toGoal >= 100) return "";
  const pct = 100 - toGoal;
  // Inside the 20 the fill goes red, the same signal the RZ badge gives.
  const rz = toGoal <= 20 ? " ffl-nfl-field-rz" : "";
  return `
    <div class="ffl-nfl-field${rz}" role="img" aria-label="${escapeHtml(`${toGoal} yards to the end zone`)}">
      <div class="ffl-nfl-field-fill" style="width:${pct}%"></div>
    </div>`;
}

/**
 * One NFL game.
 *
 * A game that has not kicked off shows its start time instead of a clock —
 * that is the only thing there is to say about it, and a blank would read as
 * missing data rather than as "not yet".
 */
function renderNflGame(game, options = {}) {
  const open = options.open === true;
  const away = game.away || {};
  const home = game.home || {};
  const awayLead = Number(away.score) > Number(home.score);
  const homeLead = Number(home.score) > Number(away.score);

  const clock =
    game.state === "pre" ? fmtKickoff(game.start_time) || "Scheduled" : game.clock_text || "";
  // Down-and-distance and the ball spot read as one phrase — "2nd & 7, DAL 19"
  // — because they are one situation, not two facts that happen to be adjacent.
  const situationText = [game.situation, game.ball_on].filter(Boolean).join(", ");
  const situation = situationText
    ? `<div class="ffl-nfl-situation">${escapeHtml(situationText)}</div>`
    : "";
  const field = renderNflField(game);
  // The last play spans the whole width under both clubs: it is about the game
  // rather than either side of it, and it is the one line here long enough to
  // need the room.
  const lastPlay = game.last_play
    ? `<div class="ffl-nfl-last">${escapeHtml(game.last_play)}</div>`
    : "";
  const panel = open
    ? `<div class="ffl-nfl-plays">${options.playsHtml || `<div class="ffl-loading">Loading plays…</div>`}</div>`
    : "";

  return `
    <div class="ffl-nfl-game${open ? " ffl-nfl-open" : ""}${game.state === "in" ? " ffl-nfl-live" : ""}">
      <div class="ffl-nfl-head" role="button" tabindex="0"
           data-game-id="${escapeHtml(game.game_id)}"
           data-plays-id="${escapeHtml(game.plays_id || "")}"
           aria-expanded="${open ? "true" : "false"}"
           aria-label="${escapeHtml(`${away.abbr || ""} ${away.score ?? ""} at ${home.abbr || ""} ${home.score ?? ""}`)}">
        <div class="ffl-nfl-teams">
          ${renderNflTeam(away, awayLead)}
          ${renderNflTeam(home, homeLead)}
        </div>
        <div class="ffl-nfl-status">
          <div class="ffl-nfl-clock${game.state === "in" ? " ffl-nfl-clock-live" : ""}">${escapeHtml(clock)}</div>
          ${situation}
        </div>
        ${field}
        ${lastPlay}
      </div>
      ${panel}
    </div>`;
}

/**
 * The "finished games" disclosure at the foot of the card.
 *
 * A caret here rather than a bare clickable line: unlike a matchup row, which
 * is a whole card's worth of obviously-interactive content, this is one line of
 * text and needs to say that it does something.
 */
function renderFinalsToggle(count, open) {
  return `
    <div class="ffl-nfl-finals${open ? " ffl-nfl-finals-open" : ""}"
         role="button" tabindex="0" data-finals-toggle="1"
         aria-expanded="${open ? "true" : "false"}"
         aria-label="${open ? "Hide" : "Show"} ${count} finished game${count === 1 ? "" : "s"}">
      <span class="ffl-nfl-finals-caret"></span>
      <span>${count} final</span>
    </div>`;
}

/** The expanded game's play list, newest first. */
function renderNflPlays(plays) {
  if (!Array.isArray(plays) || !plays.length) {
    return `<div class="ffl-empty">No plays yet.</div>`;
  }
  return `
    <ul class="ffl-nfl-playlist">
      ${plays
        .map(
          (p) => `
        <li>
          <span class="ffl-nfl-play-when">${escapeHtml(
            [p.period ? `Q${p.period}` : "", p.clock || ""].filter(Boolean).join(" ")
          )}</span>
          <span class="ffl-nfl-play-text">${escapeHtml(p.text || "")}</span>
        </li>`
        )
        .join("")}
    </ul>`;
}

function renderMatchupRow(row, options = {}) {
  const { home, away, leader } = row;
  const expanded = options.expanded === true;
  const panel = expanded ? options.panel || "roster" : "";
  const playsOpen = panel === "plays";
  const panelId = `ffl-detail-${String(row.matchup_id).replace(/[^A-Za-z0-9_-]/g, "-")}`;
  // The win bar belongs to the lineup, not to the play list: it is about where
  // the matchup is heading, which is what the lineup answers.
  const detail = expanded
    ? `<div class="ffl-row-detail" id="${panelId}">${playsOpen ? "" : renderProjections(row) + renderWinBar(row)}${
        options.detailHtml || `<div class="ffl-loading">${playsOpen ? "Loading plays…" : "Loading rosters…"}</div>`
      }</div>`
    : "";
  return `
    <div class="ffl-row-wrap${expanded ? " ffl-expanded" : ""}${playsOpen ? " ffl-plays-open" : ""}">
      <div class="ffl-row" role="button" tabindex="0"
           data-matchup-index="${escapeHtml(row.index)}"
           data-matchup-id="${escapeHtml(row.matchup_id)}"
           aria-expanded="${expanded && !playsOpen ? "true" : "false"}"
           aria-controls="${panelId}"
           aria-label="${escapeHtml(`${home.name} ${fmtPoints(home.points)}, ${away.name} ${fmtPoints(away.points)}`)}">
        ${renderTeamSide(home, leader === home.team_id, "start")}
        <div class="ffl-scores">
          <span class="ffl-score${leader === home.team_id ? " ffl-leader" : ""}">${fmtPoints(home.points)}</span>
          <span class="ffl-vs">–</span>
          <span class="ffl-score${leader === away.team_id ? " ffl-leader" : ""}">${fmtPoints(away.points)}</span>
        </div>
        ${renderTeamSide(away, leader === away.team_id, "end")}
      </div>
      ${renderRowFoot(row, playsOpen)}
      ${detail}
    </div>`;
}

/*
 * The lineup: both teams facing each other, one shared column of slot labels
 * down the middle.
 *
 * This replaced two independent <table>s side by side, which could not work at
 * phone width — two of everything (two Pos columns, two Player columns, two
 * Proj columns) simply does not fit, and the fallback was stacking one whole
 * roster above the other, which is the one thing a matchup view must not do.
 *
 * Sharing the slot column halves the horizontal cost AND makes the alignment
 * structural: one CSS grid, three tracks, so each grid row is as tall as the
 * tallest of its three cells and a player's QB always faces the opponent's QB.
 * The previous layout needed a hard-coded row height to fake that, and the
 * fake broke the moment one side's content grew.
 */

// Slot -> colour class. Scanning twenty names for "where are my receivers" is
// a colour problem, not a reading problem.
const SLOT_CLASSES = {
  QB: "qb",
  RB: "rb",
  WR: "wr",
  TE: "te",
  K: "k",
  DEF: "def",
  "D/ST": "def",
  BN: "bn",
  IR: "bn",
  "IR+": "bn",
  NA: "bn",
};

function slotClass(slot) {
  return SLOT_CLASSES[String(slot || "").toUpperCase()] || "flex";
}

function renderPlayerBlock(player, align) {
  if (!player) return `<div class="ffl-lu-block ffl-lu-${align} ffl-lu-empty"></div>`;
  const stateCls = player.game_state === "post" ? "final" : player.game_state === "in" ? "live" : "pre";
  // One number, not two: at this width the colour carries the comparison that
  // a second figure would otherwise have to spell out.
  const live = hasLive(player.live_projected, player.projected);
  const proj = live ? player.live_projected : player.projected;
  // Possession and red zone say "this player can score in the next minute",
  // which no points total can. Both only ever appear on a running game — the
  // relay leaves the last drive's possession in the row after the whistle.
  const ball = player.has_ball ? `<span class="ffl-ball" title="Has the ball">🏈</span>` : "";
  const rz = player.red_zone ? `<span class="ffl-rz" title="In the red zone">RZ</span>` : "";
  const status = player.status
    ? `<span class="ffl-status ffl-status-${escapeHtml(
        String(player.status).toLowerCase()
      )}">${escapeHtml(player.status)}</span>`
    : "";
  return `
    <div class="ffl-lu-block ffl-lu-${align} ffl-p-${stateCls}">
      <div class="ffl-lu-line">
        <span class="ffl-lu-name">${escapeHtml(player.name)}${status}${ball}${rz}</span>
        <span class="ffl-lu-pts">${fmtPoints(player.points)}</span>
      </div>
      <div class="ffl-lu-line">
        <span class="ffl-lu-meta">${escapeHtml(player.slot)}${
          player.nfl_team ? ` · ${escapeHtml(player.nfl_team)}` : ""
        }</span>
        <span class="ffl-lu-proj${live ? trendClass(player.live_projected, player.projected) : ""}">${fmtPoints(
          proj
        )}</span>
      </div>
      ${player.game ? `<div class="ffl-lu-game">${escapeHtml(player.game)}</div>` : ""}
      ${player.stat_line ? `<div class="ffl-lu-stat">${escapeHtml(player.stat_line)}</div>` : ""}
    </div>`;
}

/**
 * Both sides' players, paired by lineup position.
 *
 * Paired by INDEX rather than by slot name: both teams in a league play the
 * same lineup configuration in the same order, so index N is the same slot on
 * both sides, and a team missing a player still leaves its opponent's row in
 * place rather than shifting everything below it out of alignment.
 */
function renderLineup(sides, options = {}) {
  const key = options.bench ? "bench" : "starters";
  const home = (sides[0] && sides[0][key]) || [];
  const away = (sides[1] && sides[1][key]) || [];
  const count = Math.max(home.length, away.length);
  if (!count) return "";
  let out = "";
  for (let i = 0; i < count; i += 1) {
    const slot = (home[i] && home[i].slot) || (away[i] && away[i].slot) || "";
    out += renderPlayerBlock(home[i], "home");
    out += `<div class="ffl-lu-slot ffl-slot-${slotClass(slot)}">${escapeHtml(slot)}</div>`;
    out += renderPlayerBlock(away[i], "away");
  }
  return `<div class="ffl-lineup">${out}</div>`;
}

function renderRosters(sides) {
  if (!sides.length) return `<div class="ffl-empty">No roster available for this matchup yet.</div>`;
  const bench = renderLineup(sides, { bench: true });
  const benchCount = Math.max(((sides[0] || {}).bench || []).length, ((sides[1] || {}).bench || []).length);
  return `
    ${renderLineup(sides)}
    ${bench ? `<details class="ffl-bench"><summary>Bench (${benchCount})</summary>${bench}</details>` : ""}`;
}

function renderHistory(plays) {
  if (!plays.length) return `<div class="ffl-empty">No scoring plays recorded yet.</div>`;
  return `
    <ul class="ffl-history">
      ${plays
        .map(
          (p) => `
        <li class="${p.correction ? "ffl-correction" : ""}">
          <span class="ffl-h-player">${escapeHtml(p.player)}</span>
          <span class="ffl-h-text">${escapeHtml(p.text)}</span>
          <span class="ffl-h-delta">${escapeHtml(fmtDelta(p.delta))}</span>
        </li>`
        )
        .join("")}
    </ul>`;
}

/* ------------------------------------------------------------- base card */

class FflBaseCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) throw new Error("An `entity` is required.");
    this.config = { title: "", ...config };
    this._lastFingerprint = "";
    this._lastDataFingerprint = "";
    // Accordion state. ONE id, never a set — that is what enforces "only one
    // matchup open at a time" structurally rather than by convention. The
    // panel name rides alongside it for the same reason: a matchup now has two
    // things it can show (the lineup, its scoring plays) and exactly one of
    // them is open at a time.
    this._expandedId = null;
    this._expandedPanel = "";
    this._detailHtml = "";
    // Bumped whenever the detail payload changes, so the fingerprint guard
    // lets an async roster arrival through to the DOM.
    this._detailRev = 0;
    this._detailToken = 0;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this.card) this._build();
    this.render();
  }

  getCardSize() {
    return 4;
  }

  _build() {
    ensureCardStyles(this);
    this.card = document.createElement("ha-card");
    this.card.className = "ffl-card";
    this.content = document.createElement("div");
    this.content.className = "card-content";
    this.card.appendChild(this.content);
    this.appendChild(this.card);

    // Delegated once — innerHTML replacement orphans per-element handlers.
    this.content.addEventListener("click", (e) => this._onActivate(e));
    this.content.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        this._onActivate(e);
      }
    });
  }

  _stateObj() {
    const entityId = this.config && this.config.entity;
    if (!entityId || !this._hass || !this._hass.states) return null;
    return this._hass.states[entityId] || null;
  }

  _rows(st) {
    return Array.isArray(st.attributes.matchups) ? st.attributes.matchups : [];
  }

  /** Scalar-only. Never JSON.stringify over the play arrays. */
  _dataFingerprint(st, rows) {
    const parts = [this.constructor.name, st.state, st.attributes.week, st.attributes.partial ? 1 : 0];
    for (const r of rows) {
      parts.push(r.matchup_id, r.home.points, r.away.points, r.home.projected, r.away.projected, r.leader);
      parts.push(r.home.live_projected, r.away.live_projected, r.win_prob);
      // A correction can leave the score unchanged while the row's play text
      // changes, so the play's identity has to be in the fingerprint or the
      // guard suppresses a repaint the user is waiting on.
      parts.push(r.last_play ? r.last_play.event_id : "");
    }
    const lp = st.attributes.last_play;
    parts.push(lp ? lp.event_id : "", st.attributes.league_name, st.attributes.week);
    // The clock is not in the rows, but it IS in the open roster's game notes,
    // so a moving clock has to defeat this guard or the roster freezes at
    // whatever it said when it was opened.
    parts.push(st.attributes.active_games, st.attributes.live_tick);
    return parts.join("|");
  }

  render() {
    const st = this._stateObj();
    if (!st) {
      this._paint(
        `<div class="ffl-placeholder">Entity <code>${escapeHtml(
          (this.config && this.config.entity) || ""
        )}</code> not found.</div>`,
        "missing"
      );
      return;
    }
    const rows = this._rows(st);
    const dataFingerprint = this._dataFingerprint(st, rows);
    // The open roster is live data too, so refresh it when the scores move —
    // but ONLY on a data change, or the fetch/repaint/fetch cycle never ends.
    if (dataFingerprint !== this._lastDataFingerprint && this._expandedId) {
      this._lastDataFingerprint = dataFingerprint;
      this._loadPanel(this._expandedId, this._expandedPanel, { silent: true });
    }
    this._lastDataFingerprint = dataFingerprint;

    const fingerprint = [dataFingerprint, this._expandedId || "", this._expandedPanel, this._detailRev].join("|");
    if (fingerprint === this._lastFingerprint) return;
    this._lastFingerprint = fingerprint;
    this._paintBody(st, rows);
  }

  /** Force the next render past the fingerprint guard. */
  _invalidate() {
    this._detailRev += 1;
    this.render();
  }

  _rowOptions(row) {
    return row.matchup_id === this._expandedId
      ? { expanded: true, panel: this._expandedPanel, detailHtml: this._detailHtml }
      : {};
  }

  _paint(html, fingerprint) {
    if (fingerprint !== undefined) {
      if (fingerprint === this._lastFingerprint) return;
      this._lastFingerprint = fingerprint;
    }
    this.content.innerHTML = html;
  }

  _leagueId(st) {
    return st.attributes.league_id || "";
  }

  _onActivate(event) {
    const historyEl = event.target.closest("[data-history]");
    if (historyEl) {
      this._togglePanel(historyEl.getAttribute("data-matchup-id"), "plays");
      return;
    }
    const rowEl = event.target.closest("[data-matchup-index]");
    if (rowEl) {
      this._togglePanel(rowEl.getAttribute("data-matchup-id"), "roster", {
        index: Number(rowEl.getAttribute("data-matchup-index")),
      });
    }
  }

  /**
   * Open one of this matchup's panels beneath it, or close it if it is already
   * the open one.
   *
   * Inline rather than a dialog — for the lineup because an overlay attached
   * to `document.body` could not be reached by the card's shadow-scoped
   * stylesheet, and for the play list because one interaction for both panels
   * beats two, works identically on a phone, and keeps the scores on screen
   * while you read either.
   */
  _togglePanel(matchupId, panel, options = {}) {
    if (!matchupId) return;
    if (this._expandedId === matchupId && this._expandedPanel === panel) {
      this._expandedId = null;
      this._expandedPanel = "";
      this._detailHtml = "";
      this._invalidate();
      return;
    }
    // Assigning a single id/panel pair closes whatever was open. Clear the old
    // body first so the previous panel cannot flash under the new one.
    this._expandedId = matchupId;
    this._expandedPanel = panel;
    this._detailHtml = "";
    this._invalidate();
    this._loadPanel(matchupId, panel, options);
  }

  _loadPanel(matchupId, panel, options = {}) {
    if (panel === "plays") return this._loadPlays(matchupId, options);
    return this._loadDetail(matchupId, options);
  }

  /** Guard shared by both loaders: is this response still the one wanted? */
  _stillOpen(token, matchupId, panel) {
    return token === this._detailToken && this._expandedId === matchupId && this._expandedPanel === panel;
  }

  _showPanelError(err, options) {
    // A silent background refresh must not replace a panel already on screen
    // with an error; only the initial open may.
    if (options.silent && this._detailHtml) return;
    this._detailHtml = `<div class="ffl-error">${escapeHtml(err.message || String(err))}</div>`;
    this._invalidate();
  }

  _loadDetail(matchupId, options = {}) {
    const st = this._stateObj();
    if (!st) return;
    let index = options.index;
    if (!Number.isFinite(index)) {
      const row = this._rows(st).find((r) => r.matchup_id === matchupId);
      if (!row) return;
      index = row.index;
    }

    const token = (this._detailToken += 1);
    fetchMatchupDetail(this._hass, this._leagueId(st), index)
      .then((payload) => {
        // Discard a slow response for a matchup the user has since closed or
        // swapped away from.
        if (!this._stillOpen(token, matchupId, "roster")) return;
        this._detailHtml = renderRosters((payload && payload.sides) || []);
        this._invalidate();
      })
      .catch((err) => {
        if (!this._stillOpen(token, matchupId, "roster")) return;
        this._showPanelError(err, options);
      });
  }

  _loadPlays(matchupId, options = {}) {
    const st = this._stateObj();
    if (!st) return;
    const token = (this._detailToken += 1);
    fetchPlayHistory(this._hass, this._leagueId(st), { matchupId, limit: 100 })
      .then((payload) => {
        if (!this._stillOpen(token, matchupId, "plays")) return;
        this._detailHtml = renderHistory((payload && payload.plays) || []);
        this._invalidate();
      })
      .catch((err) => {
        if (!this._stillOpen(token, matchupId, "plays")) return;
        this._showPanelError(err, options);
      });
  }
}

/* ----------------------------------------------------------- league card */

class FflLeagueScoreboardCard extends FflBaseCard {
  static getConfigElement() {
    return document.createElement(LEAGUE_EDITOR);
  }

  static getStubConfig(hass) {
    return { entity: findFflEntity(hass) };
  }

  _paintBody(st, rows) {
    if (!rows.length) {
      this._paint(`<div class="ffl-placeholder">Waiting for league data…</div>`);
      return;
    }
    const stale = st.attributes.partial ? `<div class="ffl-stale">Some rosters could not be loaded</div>` : "";
    const body = rows.map((row) => renderMatchupRow(row, this._rowOptions(row))).join("");
    this._paint(`${renderHeader(st, this.config)}<div class="ffl-rows">${body}</div>${stale}`);
  }
}

/* ------------------------------------------------------- my matchup card */

class FflMyMatchupCard extends FflBaseCard {
  static getConfigElement() {
    return document.createElement(MY_MATCHUP_EDITOR);
  }

  static getStubConfig(hass) {
    return { entity: findFflEntity(hass) };
  }

  /** Which team is "mine": explicit config, else a sibling my-team sensor. */
  _teamId(st) {
    if (this.config.team_id) return String(this.config.team_id);
    const hass = this._hass;
    if (!hass || !hass.states) return "";
    const leagueId = this._leagueId(st);
    // Match on attributes, never on the entity id — see findFflEntity.
    const sibling = Object.keys(hass.states).find((id) => {
      const a = hass.states[id].attributes || {};
      return id.startsWith("sensor.") && String(a.team_id || "") && String(a.league_id || "") === String(leagueId);
    });
    return sibling ? String(hass.states[sibling].attributes.team_id) : "";
  }

  _rows(st) {
    const all = super._rows(st);
    const teamId = this._teamId(st);
    if (!teamId) return all.slice(0, 1);
    const mine = all.filter((r) => String(r.home.team_id) === teamId || String(r.away.team_id) === teamId);
    return mine.length ? mine : all.slice(0, 1);
  }

  _paintBody(st, rows) {
    if (!rows.length) {
      this._paint(`<div class="ffl-placeholder">Waiting for league data…</div>`);
      return;
    }
    const row = rows[0];
    this._paint(
      `${renderHeader(st, this.config)}<div class="ffl-rows ffl-single">${renderMatchupRow(
        row,
        this._rowOptions(row)
      )}</div>`
    );
  }
}

/* ----------------------------------------------------------- ha-form editor */

/* --------------------------------------------------------- NFL games card */

class FflNflGamesCard extends FflBaseCard {
  static getConfigElement() {
    return document.createElement(NFL_GAMES_EDITOR);
  }

  static getStubConfig(hass) {
    return { entity: findNflGamesEntity(hass) };
  }

  setConfig(config) {
    super.setConfig(config);
    // `null` means "not chosen yet", which is NOT the same as false — an
    // unchosen card opens the finals only when there is nothing else to show,
    // while an explicit false keeps them shut even on an all-final slate.
    this._showFinal = null;
  }

  getCardSize() {
    return 6;
  }

  _rows(st) {
    return Array.isArray(st.attributes.games) ? st.attributes.games : [];
  }

  /**
   * Live and upcoming games first, finished ones last.
   *
   * Sorted here rather than in the sensor: the slate is served in kickoff
   * order, which is the honest general-purpose shape, and "what is still worth
   * watching" is a question about this card rather than about the data.
   *
   * Within each group the feed's kickoff order is preserved, so a live game
   * sits above a scheduled one without needing a second rule — it kicked off
   * earlier, which is exactly why it is live.
   */
  _split(rows) {
    const done = [];
    const active = [];
    for (const game of rows) (game.state === "post" ? done : active).push(game);
    return { active, done };
  }

  /** Are the finished games showing? Unchosen opens only on an empty slate. */
  _finalsOpen(activeCount) {
    if (this._showFinal === null || this._showFinal === undefined) return activeCount === 0;
    return this._showFinal === true;
  }

  /** Scalar-only, like its sibling — never stringify the games array. */
  _dataFingerprint(st, rows) {
    const parts = [this.constructor.name, st.state, st.attributes.live_tick, rows.length];
    for (const g of rows) {
      const a = g.away || {};
      const h = g.home || {};
      parts.push(g.game_id, g.state, g.clock_text, g.situation, g.yards_to_goal, g.last_play);
      parts.push(a.score, h.score, a.has_ball ? 1 : 0, h.has_ball ? 1 : 0);
      parts.push(a.red_zone ? 1 : 0, h.red_zone ? 1 : 0);
    }
    return parts.join("|");
  }

  _onActivate(event) {
    const finals = event.target.closest("[data-finals-toggle]");
    if (finals) {
      const st = this._stateObj();
      const activeCount = st ? this._split(this._rows(st)).active.length : 0;
      // Flip against what is actually showing, so the first click on an
      // auto-opened list closes it rather than appearing to do nothing.
      this._showFinal = !this._finalsOpen(activeCount);
      this._invalidate();
      return;
    }
    const head = event.target.closest("[data-game-id]");
    if (!head) return;
    this._togglePanel(head.getAttribute("data-game-id"), "plays", {
      playsId: head.getAttribute("data-plays-id"),
    });
  }

  /**
   * Fetch one game's plays.
   *
   * Overrides the matchup loaders wholesale: this card's panel is a different
   * payload from a different command, and the only thing worth sharing is the
   * token guard that discards a response for a panel the reader already closed.
   */
  _loadPanel(gameId, panel, options = {}) {
    const st = this._stateObj();
    const playsId = options.playsId || this._playsIdFor(gameId);
    if (!st || !playsId) {
      this._detailHtml = `<div class="ffl-empty">No play feed for this game.</div>`;
      this._invalidate();
      return;
    }
    const token = ++this._detailToken;
    fetchNflPlays(this._hass, this._leagueId(st), playsId)
      .then((res) => {
        if (!this._stillOpen(token, gameId, panel)) return;
        this._detailHtml = renderNflPlays(res && res.plays);
        this._invalidate();
      })
      .catch((err) => {
        if (!this._stillOpen(token, gameId, panel)) return;
        this._detailHtml = `<div class="ffl-error">${escapeHtml(String(err && err.message ? err.message : err))}</div>`;
        this._invalidate();
      });
  }

  _playsIdFor(gameId) {
    const st = this._stateObj();
    if (!st) return "";
    const found = this._rows(st).find((g) => String(g.game_id) === String(gameId));
    return found ? found.plays_id || "" : "";
  }

  _renderGames(games) {
    return games
      .map((game) =>
        renderNflGame(game, {
          open: this._expandedId === String(game.game_id),
          playsHtml: this._expandedId === String(game.game_id) ? this._detailHtml : "",
        })
      )
      .join("");
  }

  _paintBody(st, rows) {
    if (!rows.length) {
      this._paint(`<div class="ffl-placeholder">Waiting for the NFL schedule…</div>`);
      return;
    }
    const live = Number(st.attributes.active_games) || 0;
    const header = `
      <div class="ffl-header">
        <span class="ffl-header-name">${escapeHtml(this.config.title || "NFL Games")}</span>
        ${live ? `<span class="ffl-header-live">${live} live</span>` : ""}
        <span class="ffl-header-week">${rows.length} games</span>
      </div>`;

    const { active, done } = this._split(rows);
    const open = this._finalsOpen(active.length);
    const finals = done.length
      ? `${renderFinalsToggle(done.length, open)}${open ? this._renderGames(done) : ""}`
      : "";
    this._paint(`${header}<div class="ffl-nfl-games">${this._renderGames(active)}${finals}</div>`);
  }
}

class FflBaseEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...(config || {}) };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._form) this._form.hass = hass;
  }

  _schema() {
    return [];
  }

  _render() {
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (s) =>
        ({
          entity: "Scoreboard entity",
          title: "Title",
          team_id: "Your team id",
        })[s.name] || s.name;
      this._form.addEventListener("value-changed", (ev) => {
        this._config = ev.detail.value;
        this.dispatchEvent(
          new CustomEvent("config-changed", {
            detail: { config: this._config },
            bubbles: true,
            composed: true,
          })
        );
      });
      this.appendChild(this._form);
    }
    this._form.schema = this._schema();
    this._form.data = this._config;
    if (this._hass) this._form.hass = this._hass;
  }
}

const ENTITY_SELECTOR = {
  name: "entity",
  required: true,
  selector: { entity: { domain: "sensor", integration: DOMAIN } },
};

class FflLeagueEditor extends FflBaseEditor {
  _schema() {
    return [ENTITY_SELECTOR, { name: "title", selector: { text: {} } }];
  }
}

class FflNflGamesEditor extends FflBaseEditor {
  _schema() {
    return [ENTITY_SELECTOR, { name: "title", selector: { text: {} } }];
  }
}

class FflMyMatchupEditor extends FflBaseEditor {
  _schema() {
    return [ENTITY_SELECTOR, { name: "title", selector: { text: {} } }, { name: "team_id", selector: { text: {} } }];
  }
}

/* --------------------------------------------------------------- styles */

const CARD_CSS = `
  .ffl-card { overflow: hidden; }
  .ffl-card .card-content { padding: 12px 16px 8px; }
  .ffl-title { font-size: 1.1rem; font-weight: 600; color: var(--primary-text-color); margin-bottom: 8px; }
  .ffl-placeholder { color: var(--secondary-text-color); font-size: 0.95rem; }
  .ffl-placeholder code { background: var(--secondary-background-color); border-radius: 4px; padding: 1px 4px; }

  .ffl-rows { display: flex; flex-direction: column; gap: 2px; }
  .ffl-row {
    display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 8px;
    padding: 5px 6px; border-radius: 8px; cursor: pointer;
    border-bottom: 1px solid var(--divider-color);
  }
  /* The divider belongs to the WRAPPER, not the row — with a play line
     present it has to sit under both, or every scoring matchup grows a rule
     through its middle. */
  .ffl-row { border-bottom: none; }
  .ffl-row-wrap { border-bottom: 1px solid var(--divider-color); }
  .ffl-rows .ffl-row-wrap:last-child { border-bottom: none; }
  .ffl-row:hover, .ffl-row:focus-visible { background: var(--secondary-background-color); outline: none; }
  .ffl-single .ffl-row { padding: 8px 6px; }

  /* Three tracks so the play line sits on its own scoring side — column 1 for
     home, column 3 for away. The foot is not rendered at all when there is no
     play, so a quiet matchup costs nothing. */
  .ffl-row-foot {
    display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    align-items: center; gap: 6px; padding: 0 6px 4px;
  }
  .ffl-row-play {
    /* Row pinned explicitly, and it matters: with the projections beside it,
       an AWAY play takes column 3 and pushes grid's placement cursor past
       column 2, so a sibling without its own row lands on a second line. That
       showed up on exactly the matchups whose last score went right, which
       read as a data problem rather than a layout one. */
    grid-column: 1; grid-row: 1;
    min-width: 0; padding: 1px 4px; font-size: 0.8rem; line-height: 1.2;
    color: var(--secondary-text-color); cursor: pointer; border-radius: 6px;
  }
  .ffl-row-play:hover, .ffl-row-play:focus-visible { background: var(--secondary-background-color); outline: none; }
  /* Reads inward from the scoring team's edge — position alone says which
     side scored, the same way the team names do. */
  .ffl-play-home { text-align: start; }
  .ffl-play-away { grid-column: 3; text-align: end; }
  /* Wraps like the team name above it, and for the same reason: a phone's
     side track is ~100px and an ellipsis there ate the yardage, which is the
     part worth reading. The player and the result are inline-blocks, which
     is what makes the break land between them: an inline-block is atomic to
     the line breaker, so the result moves to the next line whole when it
     does not fit beside the name. The name never breaks internally; the
     result still may, as a last resort on a very narrow row. */
  .ffl-row-play-text { white-space: normal; overflow-wrap: anywhere; }
  .ffl-row-play-who { display: inline-block; white-space: nowrap; }
  .ffl-row-play-what { display: inline-block; }
  /* A play that took points away — a pick, a fumble, a loss — reads in the
     error colour now that there is no red delta to carry that. */
  .ffl-row-play.ffl-correction { color: var(--error-color); }

  /* ---- NFL games ---- */
  .ffl-nfl-games { display: flex; flex-direction: column; gap: 2px; }
  .ffl-nfl-game { border-bottom: 1px solid var(--divider-color); }
  .ffl-nfl-games .ffl-nfl-game:last-child { border-bottom: none; }
  /* Two columns: the clubs stack on the left, the clock and situation read
     down the right. The progress bar spans both because it is about the game,
     not about either side of it. */
  .ffl-nfl-head {
    display: grid; grid-template-columns: minmax(0, 1fr) auto;
    align-items: center; gap: 8px;
    padding: 6px; border-radius: 8px; cursor: pointer;
  }
  .ffl-nfl-head:hover, .ffl-nfl-head:focus-visible {
    background: var(--secondary-background-color); outline: none;
  }
  .ffl-nfl-teams { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
  /* Medium (500) is the card's floor, not regular: the type here is small,
     and at 400 on a tablet it read as faint rather than as quiet. Both clubs
     are in the PRIMARY colour — the leader is told apart by weight alone.
     They used to be secondary with only the leader lifted, which on a dark
     theme left a tied game as two lines of mid-grey, the dullest thing on
     the dashboard. */
  .ffl-nfl-team {
    display: flex; align-items: baseline; gap: 6px; min-width: 0;
    color: var(--primary-text-color); font-weight: 500;
  }
  .ffl-nfl-team.ffl-nfl-lead { font-weight: 700; }
  .ffl-nfl-abbr { font-size: 0.9rem; letter-spacing: .02em; }
  .ffl-nfl-score {
    margin-inline-start: auto; font-size: 0.95rem; font-variant-numeric: tabular-nums;
  }
  .ffl-poss { font-size: 0.7rem; line-height: 1; }
  .ffl-rz {
    font-size: 0.6rem; font-weight: 700; letter-spacing: .04em;
    color: var(--error-color, #db4437); border: 1px solid currentColor;
    border-radius: 4px; padding: 0 3px;
  }
  .ffl-nfl-status { text-align: end; flex: none; }
  .ffl-nfl-clock { font-size: 0.78rem; font-weight: 500; color: var(--secondary-text-color); white-space: nowrap; }
  .ffl-nfl-clock-live { color: var(--primary-text-color); font-weight: 700; }
  .ffl-nfl-situation { font-size: 0.7rem; font-weight: 500; color: var(--primary-text-color); white-space: nowrap; }
  /* The field. Spans both tracks like the play below it. Track in the
     divider colour, fill in the primary — the same pair the win bar uses —
     with no yard markings: it is there to be read at a glance, and the yard
     line beside it is the number. */
  .ffl-nfl-field {
    grid-column: 1 / -1; height: 4px; margin-top: 4px; border-radius: 2px;
    background: var(--divider-color); overflow: hidden;
  }
  .ffl-nfl-field-fill { height: 100%; border-radius: 2px; background: var(--primary-color); }
  .ffl-nfl-field-rz .ffl-nfl-field-fill { background: var(--error-color, #db4437); }
  /* Spans both tracks: the play belongs to the game, not to either club.
     Set exactly like a row of the expanded play list (size, weight, colour)
     — it IS that list's newest row, shown early. */
  .ffl-nfl-last {
    grid-column: 1 / -1; margin-top: 2px;
    font-size: 0.78rem; font-weight: 500; color: var(--primary-text-color);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .ffl-nfl-finals {
    display: flex; align-items: center; justify-content: center; gap: 6px;
    padding: 6px; margin-top: 2px; cursor: pointer; border-radius: 8px;
    font-size: 0.75rem; font-weight: 500; color: var(--secondary-text-color);
    text-transform: uppercase; letter-spacing: .04em;
  }
  .ffl-nfl-finals:hover, .ffl-nfl-finals:focus-visible {
    background: var(--secondary-background-color); outline: none;
  }
  .ffl-nfl-finals-caret {
    width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid currentColor;
    transition: transform 120ms ease-in-out;
  }
  .ffl-nfl-finals-open .ffl-nfl-finals-caret { transform: rotate(180deg); }
  /* A finished game is still readable, just not competing with a live one. */
  .ffl-nfl-finals-open ~ .ffl-nfl-game .ffl-nfl-abbr { color: var(--secondary-text-color); }

  .ffl-nfl-plays { padding: 2px 6px 10px; }
  .ffl-nfl-playlist { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
  .ffl-nfl-playlist li { display: flex; gap: 8px; font-size: 0.78rem; align-items: baseline; }
  .ffl-nfl-play-when {
    flex: none; min-width: 4.2em; color: var(--secondary-text-color);
    font-variant-numeric: tabular-nums;
  }
  .ffl-nfl-play-text { min-width: 0; font-weight: 500; color: var(--primary-text-color); }

  .ffl-header {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 12px; padding: 2px 6px 10px; margin-bottom: 6px;
    border-bottom: 1px solid var(--divider-color);
  }
  .ffl-header-name {
    font-size: 1.15rem; font-weight: 600; color: var(--primary-text-color);
    min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .ffl-header-live {
    flex: none; font-size: 0.68rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: .04em; color: var(--success-color, #43a047);
    border: 1px solid currentColor; border-radius: 10px; padding: 1px 6px;
    white-space: nowrap;
  }
  .ffl-header-week {
    flex: none; font-size: 0.8rem; text-transform: uppercase; letter-spacing: .04em;
    color: var(--secondary-text-color);
  }

  .ffl-expanded > .ffl-row { background: var(--secondary-background-color); }
  .ffl-plays-open > .ffl-row { background: none; }
  .ffl-row-play.ffl-play-open { background: var(--secondary-background-color); }

  .ffl-row-detail { padding: 4px 2px 12px; }

  /* Same three tracks as the row and the play line, so each side's numbers
     land under their own team instead of floating mid-card. */
  .ffl-projs {
    display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    align-items: start; gap: 8px; padding: 0 6px 6px;
  }
  .ffl-proj-start { text-align: start; }
  .ffl-proj-end { text-align: end; }

  .ffl-team { min-width: 0; }
  .ffl-team-start { text-align: start; }
  .ffl-team-end { text-align: end; }
  /* Names WRAP rather than truncate: a 1fr track on a phone is ~90px, and
     an ellipsis there cut most of the league down to its first two words.
     No line cap on purpose — Yahoo limits a team name to 20 characters, so
     two lines hold any name at any width this card is used at, and a clamp
     would only add an engine-specific -webkit-box path for nothing.
     overflow-wrap: anywhere is for the one-word name with nowhere to
     break; without it the word runs under the score instead of wrapping. */
  .ffl-team-name {
    font-size: 0.85rem; line-height: 1.2; font-weight: 500; color: var(--primary-text-color);
    white-space: normal; overflow-wrap: anywhere;
  }
  .ffl-team.ffl-leader .ffl-team-name { font-weight: 700; }
  .ffl-team-proj { font-size: 0.72rem; color: var(--secondary-text-color); }
  .ffl-team-live { font-size: 0.78rem; font-weight: 600; font-variant-numeric: tabular-nums; }

  /* One colour vocabulary for "against expectation", used by team totals and
     by individual players alike: ahead of the pre-game projection is green,
     behind it is red, exactly level is grey — which before kickoff is
     everything, and once a game is running is almost nothing. */
  .ffl-up { color: var(--success-color, #43a047); }
  .ffl-down { color: var(--error-color, #db4437); }
  .ffl-flat { color: var(--secondary-text-color); }

  /* Projected winner. Our own estimate, not Yahoo's licensed market number. */
  .ffl-winbar {
    display: flex; align-items: center; gap: 8px;
    padding: 2px 2px 10px; font-size: 0.75rem; font-variant-numeric: tabular-nums;
  }
  .ffl-win-pct { flex: 0 0 auto; color: var(--secondary-text-color); }
  .ffl-win-pct.ffl-win-fav { color: var(--primary-text-color); font-weight: 700; }
  .ffl-win-track {
    flex: 1 1 auto; display: flex; gap: 2px; height: 6px;
    border-radius: 3px; overflow: hidden;
  }
  .ffl-win-seg { background: var(--divider-color); border-radius: 3px; min-width: 2px; }
  .ffl-win-seg.ffl-win-fav { background: var(--success-color, #43a047); }

  .ffl-scores { display: flex; align-items: baseline; gap: 6px; font-variant-numeric: tabular-nums; }

  /* The projections take the foot's MIDDLE track — the one the chevron used to
     hold — so they sit centred under the scores without costing a row. The
     explicit grid-row is load-bearing again now that the play has a sibling:
     an AWAY play takes column 3 and pushes grid's placement cursor past
     column 2, which would drop these onto a second line and undo the point. */
  .ffl-rowprojs {
    grid-column: 2; grid-row: 1;
    display: flex; align-items: baseline; gap: 10px;
    font-variant-numeric: tabular-nums;
  }
  .ffl-rowproj { font-size: 0.72rem; font-weight: 600; line-height: 1.1; }
  .ffl-score { font-size: 0.9rem; color: var(--secondary-text-color); }
  .ffl-score.ffl-leader { color: var(--primary-text-color); font-weight: 700; }
  .ffl-vs { color: var(--secondary-text-color); font-size: 0.8rem; }

  .ffl-stale { font-size: 0.75rem; color: var(--warning-color); padding: 4px 6px; }

  .ffl-loading, .ffl-empty { color: var(--secondary-text-color); padding: 12px 0; }
  .ffl-error { color: var(--error-color); padding: 12px 0; }

  .ffl-lineup {
    display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    column-gap: 6px; align-items: stretch;
  }
  .ffl-lu-block {
    min-width: 0; padding: 5px 4px; border-top: 1px solid var(--divider-color);
    display: flex; flex-direction: column; justify-content: center; gap: 1px;
  }
  .ffl-lu-slot {
    display: flex; align-items: center; justify-content: center;
    border-top: 1px solid var(--divider-color);
    font-size: 0.65rem; font-weight: 700; letter-spacing: .03em;
    color: var(--primary-text-color); min-width: 2.9em; padding: 0 4px;
  }
  .ffl-lineup > :nth-child(-n+3) { border-top: none; }

  .ffl-lu-line { display: flex; align-items: baseline; gap: 6px; min-width: 0; }
  /* The away side is the same markup read backwards — mirroring in CSS keeps
     one block renderer instead of two that can drift apart. */
  .ffl-lu-away { text-align: end; }
  .ffl-lu-away .ffl-lu-line { flex-direction: row-reverse; }
  .ffl-lu-name {
    flex: 1 1 auto; min-width: 0; font-weight: 600; font-size: 0.82rem;
    color: var(--primary-text-color);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .ffl-lu-pts {
    flex: 0 0 auto; font-size: 0.82rem; font-weight: 700;
    font-variant-numeric: tabular-nums; color: var(--primary-text-color);
  }
  .ffl-lu-meta {
    flex: 1 1 auto; min-width: 0; font-size: 0.7rem; color: var(--secondary-text-color);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .ffl-lu-proj { flex: 0 0 auto; font-size: 0.72rem; font-variant-numeric: tabular-nums; color: var(--secondary-text-color); }
  .ffl-lu-game, .ffl-lu-stat {
    font-size: 0.66rem; color: var(--secondary-text-color);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .ffl-lu-stat { font-style: italic; }

  .ffl-ball { font-size: 0.7em; margin-inline-start: 4px; }
  .ffl-rz {
    margin-inline-start: 4px; padding: 0 3px; border-radius: 3px;
    font-size: 0.6rem; font-weight: 700; letter-spacing: .03em;
    color: #fff; background: var(--error-color, #db4437); vertical-align: middle;
  }
  .ffl-status {
    margin-inline-start: 4px; padding: 0 3px; border-radius: 3px;
    font-size: 0.6rem; font-weight: 700; vertical-align: middle;
    color: var(--error-color, #db4437);
    background: color-mix(in srgb, var(--error-color, #db4437) 18%, transparent);
  }
  /* Out and IR are settled facts, not risks — muted rather than alarming. */
  .ffl-status-o, .ffl-status-ir { color: var(--secondary-text-color); background: none; }

  /* Slot colours — position at a glance. */
  .ffl-slot-qb { color: #5b9bff; }
  .ffl-slot-rb { color: #3ec46d; }
  .ffl-slot-wr { color: #ffa23e; }
  .ffl-slot-te { color: #ef7fc0; }
  .ffl-slot-k { color: #b98cff; }
  .ffl-slot-def { color: #8fd6d0; }
  .ffl-slot-flex { color: #9aa6b2; }
  .ffl-slot-bn { color: var(--secondary-text-color); }

  /* A player whose NFL game is in progress is the only one whose number can
     still move, which is the thing worth spotting at a glance. Shading the
     whole block rather than colouring text: the points already use colour. */
  .ffl-p-live { background: rgba(76, 175, 80, 0.13); }
  .ffl-p-live { background: color-mix(in srgb, var(--success-color, #43a047) 14%, transparent); }
  .ffl-p-pre .ffl-lu-name, .ffl-p-pre .ffl-lu-pts { opacity: .65; }

  .ffl-bench summary { cursor: pointer; font-size: 0.75rem; color: var(--secondary-text-color); padding: 8px 4px 4px; }

  .ffl-history { list-style: none; margin: 0; padding: 0 4px; }
  .ffl-history li {
    display: flex; gap: 8px; align-items: baseline;
    padding: 6px 2px; border-bottom: 1px solid var(--divider-color); font-size: 0.85rem;
  }
  .ffl-h-player { font-weight: 600; flex: 0 0 auto; }
  .ffl-h-text { flex: 1 1 auto; color: var(--secondary-text-color); }
  .ffl-h-delta { flex: 0 0 auto; font-weight: 700; color: var(--success-color); font-variant-numeric: tabular-nums; }
  .ffl-history li.ffl-correction .ffl-h-delta { color: var(--error-color); }
`;

/* ------------------------------------------------------------- registration */

/*
 * Registration is guarded so the bundle can also be `require`d by the Node test
 * runner, which has no DOM. The renderers above are pure string builders, so
 * guarding here is all that is needed to unit-test them with zero dependencies
 * — see tests/test_cards.js. In a browser this branch always runs.
 */
if (typeof customElements !== "undefined") {
  customElements.define(MY_MATCHUP_TAG, FflMyMatchupCard);
  customElements.define(LEAGUE_TAG, FflLeagueScoreboardCard);
  customElements.define(NFL_GAMES_TAG, FflNflGamesCard);
  customElements.define(MY_MATCHUP_EDITOR, FflMyMatchupEditor);
  customElements.define(LEAGUE_EDITOR, FflLeagueEditor);
  customElements.define(NFL_GAMES_EDITOR, FflNflGamesEditor);

  window.customCards = window.customCards || [];
  for (const entry of [
    { type: MY_MATCHUP_TAG, name: "Fantasy Football — My Matchup", description: "Your Yahoo fantasy matchup." },
    { type: LEAGUE_TAG, name: "Fantasy Football — League Scoreboard", description: "Every matchup in your league." },
    { type: NFL_GAMES_TAG, name: "Fantasy Football — NFL Games", description: "The real NFL slate, live." },
  ]) {
    if (!window.customCards.find((c) => c.type === entry.type)) {
      window.customCards.push({ ...entry, preview: true, documentationURL: DOCS_URL });
    }
  }

  console.info(`%c YAHOO-FANTASY-FOOTBALL-CARDS %c ${CARD_VERSION} `, "color:white;background:#6001d2", "");
}

// Test-only surface. `typeof module` is undefined in a browser ES module, so
// this is inert in production.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    CARD_VERSION,
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
    renderNflTeam,
    renderNflPlays,
    renderFinalsToggle,
    fmtKickoff,
    findFflEntity,
    findNflGamesEntity,
    FflNflGamesCard,
  };
}
