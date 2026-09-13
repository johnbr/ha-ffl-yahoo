/* Yahoo Fantasy Football cards for Home Assistant. */
const CARD_VERSION = "0.6.0"; // x-release-please-version

const MY_MATCHUP_TAG = "ffl-my-matchup-card";
const LEAGUE_TAG = "ffl-league-scoreboard-card";
const MY_MATCHUP_EDITOR = "ffl-my-matchup-card-editor";
const LEAGUE_EDITOR = "ffl-league-scoreboard-card-editor";
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

/* -------------------------------------------------------------- websocket */

function callWS(hass, message) {
  if (!hass || typeof hass.callWS !== "function") {
    return Promise.reject(new Error("Home Assistant connection unavailable"));
  }
  return hass.callWS(message);
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
  const text = `<span class="ffl-row-play-text">${escapeHtml(play.short_text || play.text)}</span>`;
  const delta = `<span class="ffl-row-play-delta">${escapeHtml(fmtDelta(play.delta))}</span>`;
  return `
    <div class="ffl-row-play ffl-play-${side}${play.correction ? " ffl-correction" : ""}${open ? " ffl-play-open" : ""}"
         role="button" tabindex="0" data-history="1"
         data-matchup-id="${escapeHtml(matchupId || "")}"
         aria-expanded="${open ? "true" : "false"}"
         aria-label="Show scoring play history">
      ${side === "away" ? text + delta : delta + text}
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
      <div class="ffl-row-foot">
        ${renderRowPlay(row.last_play, row.matchup_id, playsOpen)}
        <div class="ffl-row-toggle" aria-hidden="true"
             data-matchup-index="${escapeHtml(row.index)}"
             data-matchup-id="${escapeHtml(row.matchup_id)}">
          <span class="ffl-chevron"></span>
        </div>
      </div>
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

  /* Play line and chevron share ONE line so the card does not grow a strip per
     matchup. Three tracks, not flex: the chevron sits in the middle track and
     is therefore centred on the card whichever side the play is on (or when
     there is no play at all), which a flex row cannot promise. */
  .ffl-row-foot {
    display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    align-items: center; gap: 6px; padding: 0 6px 4px;
  }
  .ffl-row-play {
    grid-column: 1;
    display: flex; align-items: baseline; gap: 6px;
    padding: 1px 4px; font-size: 0.8rem; color: var(--secondary-text-color);
    cursor: pointer; border-radius: 6px;
  }
  .ffl-play-away { grid-column: 3; }
  .ffl-row-play:hover, .ffl-row-play:focus-visible { background: var(--secondary-background-color); outline: none; }
  /* The delta sits on the OUTSIDE edge of the team that earned it, with the
     description reading inward — position alone then says which side scored. */
  .ffl-play-home { justify-content: flex-start; }
  .ffl-play-away { justify-content: flex-end; }
  .ffl-play-unknown { justify-content: space-between; }
  /* The play text is the variable-length part, so it is the part that
     truncates; the delta is short and always worth showing in full. */
  .ffl-row-play-text { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ffl-play-unknown .ffl-row-play-text { flex: 1 1 auto; }
  .ffl-row-play-delta { flex: none; font-variant-numeric: tabular-nums; color: var(--primary-color); }
  .ffl-row-play.ffl-correction .ffl-row-play-delta { color: var(--error-color); }

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

  /* The chevron is its own full-width strip under the row rather than a fourth
     grid track, so it stays centred under the whole card and sits below the
     play line instead of above it. */
  .ffl-row-toggle {
    grid-column: 2;
    display: flex; align-items: center; justify-content: center;
    padding: 4px 8px; cursor: pointer;
  }
  .ffl-row-toggle:hover .ffl-chevron { border-top-color: var(--primary-text-color); }
  .ffl-chevron {
    width: 0; height: 0;
    border-left: 5px solid transparent; border-right: 5px solid transparent;
    border-top: 6px solid var(--secondary-text-color);
    transition: transform 120ms ease-in-out;
  }
  .ffl-expanded .ffl-chevron { transform: rotate(180deg); }
  /* The chevron controls the LINEUP panel, so it must not read as open when
     the play list is what is showing. */
  .ffl-plays-open .ffl-chevron { transform: none; }
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
  .ffl-team-name {
    font-weight: 500; color: var(--primary-text-color);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
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
  .ffl-score { font-size: 1.15rem; color: var(--secondary-text-color); }
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
  customElements.define(MY_MATCHUP_EDITOR, FflMyMatchupEditor);
  customElements.define(LEAGUE_EDITOR, FflLeagueEditor);

  window.customCards = window.customCards || [];
  for (const entry of [
    { type: MY_MATCHUP_TAG, name: "Fantasy Football — My Matchup", description: "Your Yahoo fantasy matchup." },
    { type: LEAGUE_TAG, name: "Fantasy Football — League Scoreboard", description: "Every matchup in your league." },
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
    findFflEntity,
  };
}
