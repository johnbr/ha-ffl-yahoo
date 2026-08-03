/* Yahoo Fantasy Football cards for Home Assistant. */
const CARD_VERSION = "0.5.0"; // x-release-please-version

const MY_MATCHUP_TAG = "ffl-my-matchup-card";
const LEAGUE_TAG = "ffl-league-scoreboard-card";
const MY_MATCHUP_EDITOR = "ffl-my-matchup-card-editor";
const LEAGUE_EDITOR = "ffl-league-scoreboard-card-editor";
const DOCS_URL = "https://github.com/johnbr/ha-ffl-yahoo";
const STYLE_CLASS = "ffl-card-style";
const DOMAIN = "yahoo_fantasy_football";

/*
 * Both cards share this one bundle so they can share the render helpers, the
 * roster/history overlays and the CSS — two custom elements, one Lovelace
 * resource.
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

function ensureCardStyles(host) {
  if (host.querySelector(`.${STYLE_CLASS}`)) return;
  const style = document.createElement("style");
  style.className = STYLE_CLASS;
  style.textContent = CARD_CSS;
  host.appendChild(style);
}

function findFflEntity(hass) {
  if (!hass || !hass.states) return "";
  const match = Object.keys(hass.states).find(
    (id) => id.startsWith("sensor.ffl_") && id.endsWith("_scoreboard")
  );
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
  };
  if (options.matchupId) message.matchup_id = options.matchupId;
  if (options.teamKey) message.team_key = options.teamKey;
  return callWS(hass, message);
}

/* ---------------------------------------------------------------- overlay */

/*
 * A hand-rolled dialog rather than <ha-dialog> or browser_mod: zero imports,
 * works in every HA context, and browser_mod's `autoclose` is a hover
 * misfeature that dismisses on the first mousemove.
 */
class FflOverlay {
  constructor() {
    this._root = null;
    this._token = 0;
    this._lastFocus = null;
    this._onKeyDown = this._onKeyDown.bind(this);
  }

  /** Monotonic token so a slow response can never overwrite a newer request. */
  nextToken() {
    this._token += 1;
    return this._token;
  }

  isCurrent(token) {
    return token === this._token && this._root !== null;
  }

  open(title) {
    if (this._root) this.close();
    this._lastFocus = document.activeElement;

    const root = document.createElement("div");
    root.className = "ffl-overlay";
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-label", title || "Details");
    root.innerHTML = `
      <div class="ffl-overlay-backdrop"></div>
      <div class="ffl-overlay-panel" tabindex="-1">
        <div class="ffl-overlay-head">
          <div class="ffl-overlay-title">${escapeHtml(title || "")}</div>
          <button class="ffl-overlay-close" aria-label="Close">&times;</button>
        </div>
        <div class="ffl-overlay-body"><div class="ffl-loading">Loading…</div></div>
      </div>`;

    // Backdrop and the ✕ close; clicks inside the panel must not.
    root.querySelector(".ffl-overlay-backdrop").addEventListener("click", () => this.close());
    root.querySelector(".ffl-overlay-close").addEventListener("click", () => this.close());
    document.addEventListener("keydown", this._onKeyDown, true);

    document.body.appendChild(root);
    // Scroll lock, so the page behind doesn't move while the dialog is open.
    this._prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    root.querySelector(".ffl-overlay-panel").focus();

    this._root = root;
    return root;
  }

  setBody(html) {
    if (!this._root) return;
    this._root.querySelector(".ffl-overlay-body").innerHTML = html;
  }

  setTitle(title) {
    if (!this._root) return;
    this._root.querySelector(".ffl-overlay-title").textContent = title || "";
  }

  body() {
    return this._root ? this._root.querySelector(".ffl-overlay-body") : null;
  }

  close() {
    // Bump the token so any in-flight response is discarded rather than
    // painting into a dialog the user already dismissed.
    this._token += 1;
    document.removeEventListener("keydown", this._onKeyDown, true);
    if (this._root && this._root.parentNode) this._root.parentNode.removeChild(this._root);
    this._root = null;
    document.body.style.overflow = this._prevOverflow || "";
    if (this._lastFocus && typeof this._lastFocus.focus === "function") {
      this._lastFocus.focus();
    }
    this._lastFocus = null;
  }

  _onKeyDown(event) {
    if (!this._root) return;
    if (event.key === "Escape") {
      event.stopPropagation();
      this.close();
      return;
    }
    if (event.key !== "Tab") return;
    // Keep focus inside the dialog.
    const focusable = this._root.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    );
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
}

const overlay = new FflOverlay();

/* ------------------------------------------------------------- rendering */

function renderTeamSide(team, isLeader, align) {
  const cls = `ffl-team ffl-team-${align}${isLeader ? " ffl-leader" : ""}`;
  return `
    <div class="${cls}">
      <div class="ffl-team-name">${escapeHtml(team.name)}</div>
      <div class="ffl-team-proj">proj ${fmtPoints(team.projected)}</div>
    </div>`;
}

function renderMatchupRow(row) {
  const { home, away, leader } = row;
  return `
    <div class="ffl-row" role="button" tabindex="0"
         data-matchup-index="${escapeHtml(row.index)}"
         data-matchup-id="${escapeHtml(row.matchup_id)}"
         aria-label="${escapeHtml(`${home.name} ${fmtPoints(home.points)}, ${away.name} ${fmtPoints(away.points)}`)}">
      ${renderTeamSide(home, leader === home.team_id, "start")}
      <div class="ffl-scores">
        <span class="ffl-score${leader === home.team_id ? " ffl-leader" : ""}">${fmtPoints(home.points)}</span>
        <span class="ffl-vs">–</span>
        <span class="ffl-score${leader === away.team_id ? " ffl-leader" : ""}">${fmtPoints(away.points)}</span>
      </div>
      ${renderTeamSide(away, leader === away.team_id, "end")}
    </div>`;
}

function renderBanner(play, label) {
  if (!play) {
    return `<div class="ffl-banner ffl-banner-empty" role="button" tabindex="0" data-history="1">
              <span class="ffl-banner-label">${escapeHtml(label)}</span>
              <span class="ffl-banner-text">No scoring plays yet</span>
            </div>`;
  }
  return `
    <div class="ffl-banner${play.correction ? " ffl-correction" : ""}"
         role="button" tabindex="0" data-history="1"
         aria-label="Show scoring play history">
      <span class="ffl-banner-label">${escapeHtml(label)}</span>
      <span class="ffl-banner-text">${escapeHtml(play.text)}</span>
      <span class="ffl-banner-delta">${escapeHtml(fmtDelta(play.delta))}</span>
    </div>`;
}

function renderPlayerRow(player) {
  const stateCls =
    player.game_state === "post" ? "final" : player.game_state === "in" ? "live" : "pre";
  return `
    <tr class="ffl-p-${stateCls}">
      <td class="ffl-slot">${escapeHtml(player.slot)}</td>
      <td class="ffl-pname">
        <div>${escapeHtml(player.name)}</div>
        ${player.stat_line ? `<div class="ffl-statline">${escapeHtml(player.stat_line)}</div>` : ""}
      </td>
      <td class="ffl-game">${escapeHtml(player.game || "")}</td>
      <td class="ffl-proj">${fmtPoints(player.projected)}</td>
      <td class="ffl-pts">${fmtPoints(player.points)}</td>
    </tr>`;
}

function renderRosterSide(side) {
  const head = `
    <tr><th>Pos</th><th>Player</th><th>Game</th><th>Proj</th><th>Pts</th></tr>`;
  const starters = side.starters.map(renderPlayerRow).join("");
  const bench = side.bench.map(renderPlayerRow).join("");
  return `
    <div class="ffl-roster">
      <div class="ffl-roster-head">
        <span class="ffl-roster-name">${escapeHtml(side.name)}</span>
        <span class="ffl-roster-pts">${fmtPoints(side.points)}</span>
      </div>
      <table class="ffl-table"><thead>${head}</thead><tbody>${starters}</tbody></table>
      <details class="ffl-bench">
        <summary>Bench (${side.bench.length})</summary>
        <table class="ffl-table"><tbody>${bench}</tbody></table>
      </details>
    </div>`;
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
    this.config = { title: "", show_banner: true, ...config };
    this._lastFingerprint = "";
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
  _fingerprint(st, rows) {
    const parts = [this.constructor.name, st.state, st.attributes.week, st.attributes.partial ? 1 : 0];
    for (const r of rows) {
      parts.push(r.matchup_id, r.home.points, r.away.points, r.home.projected, r.away.projected, r.leader);
    }
    const lp = st.attributes.last_play;
    parts.push(lp ? lp.event_id : "");
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
    const fingerprint = this._fingerprint(st, rows);
    if (fingerprint === this._lastFingerprint) return;
    this._lastFingerprint = fingerprint;
    this._paintBody(st, rows);
  }

  _paint(html, fingerprint) {
    if (fingerprint !== undefined) {
      if (fingerprint === this._lastFingerprint) return;
      this._lastFingerprint = fingerprint;
    }
    const title = this.config && this.config.title;
    this.content.innerHTML =
      (title ? `<div class="ffl-title">${escapeHtml(title)}</div>` : "") + html;
  }

  _leagueId(st) {
    return st.attributes.league_id || "";
  }

  _onActivate(event) {
    const historyEl = event.target.closest("[data-history]");
    if (historyEl) {
      this._openHistory(historyEl.getAttribute("data-matchup-id") || null);
      return;
    }
    const rowEl = event.target.closest("[data-matchup-index]");
    if (rowEl) {
      this._openRoster(Number(rowEl.getAttribute("data-matchup-index")));
    }
  }

  _openRoster(index) {
    const st = this._stateObj();
    if (!st || !Number.isFinite(index)) return;
    const leagueId = this._leagueId(st);
    overlay.open("Rosters");
    const token = overlay.nextToken();

    fetchMatchupDetail(this._hass, leagueId, index)
      .then((payload) => {
        if (!overlay.isCurrent(token)) return;
        const sides = (payload && payload.sides) || [];
        if (!sides.length) {
          overlay.setBody(
            `<div class="ffl-empty">No roster available for this matchup yet.</div>`
          );
          return;
        }
        overlay.setTitle(`${sides[0].name} vs ${sides[1].name}`);
        overlay.setBody(`<div class="ffl-rosters">${sides.map(renderRosterSide).join("")}</div>`);
      })
      .catch((err) => {
        if (!overlay.isCurrent(token)) return;
        overlay.setBody(`<div class="ffl-error">${escapeHtml(err.message || String(err))}</div>`);
      });
  }

  _openHistory(matchupId) {
    const st = this._stateObj();
    if (!st) return;
    const leagueId = this._leagueId(st);
    overlay.open("Scoring plays");
    const token = overlay.nextToken();

    fetchPlayHistory(this._hass, leagueId, { matchupId, limit: 100 })
      .then((payload) => {
        if (!overlay.isCurrent(token)) return;
        overlay.setBody(renderHistory((payload && payload.plays) || []));
      })
      .catch((err) => {
        if (!overlay.isCurrent(token)) return;
        overlay.setBody(`<div class="ffl-error">${escapeHtml(err.message || String(err))}</div>`);
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
    const banner = this.config.show_banner
      ? renderBanner(st.attributes.last_play, "Last play")
      : "";
    const stale = st.attributes.partial
      ? `<div class="ffl-stale">Some rosters could not be loaded</div>`
      : "";
    this._paint(
      `<div class="ffl-rows">${rows.map(renderMatchupRow).join("")}</div>${stale}${banner}`
    );
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
    const sibling = Object.keys(hass.states).find(
      (id) =>
        id.startsWith("sensor.ffl_") &&
        id.endsWith("_my_team") &&
        String(hass.states[id].attributes.team_id || "") &&
        id.includes(String(leagueId))
    );
    return sibling ? String(hass.states[sibling].attributes.team_id) : "";
  }

  _rows(st) {
    const all = super._rows(st);
    const teamId = this._teamId(st);
    if (!teamId) return all.slice(0, 1);
    const mine = all.filter(
      (r) => String(r.home.team_id) === teamId || String(r.away.team_id) === teamId
    );
    return mine.length ? mine : all.slice(0, 1);
  }

  _paintBody(st, rows) {
    if (!rows.length) {
      this._paint(`<div class="ffl-placeholder">Waiting for league data…</div>`);
      return;
    }
    const row = rows[0];
    const banner = this.config.show_banner
      ? renderBanner(st.attributes.last_play, "Last play")
      : "";
    this._paint(`<div class="ffl-rows ffl-single">${renderMatchupRow(row)}</div>${banner}`);
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
          show_banner: "Show last-play banner",
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
    return [
      ENTITY_SELECTOR,
      { name: "title", selector: { text: {} } },
      { name: "show_banner", selector: { boolean: {} } },
    ];
  }
}

class FflMyMatchupEditor extends FflBaseEditor {
  _schema() {
    return [
      ENTITY_SELECTOR,
      { name: "title", selector: { text: {} } },
      { name: "team_id", selector: { text: {} } },
      { name: "show_banner", selector: { boolean: {} } },
    ];
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
    padding: 8px 6px; border-radius: 8px; cursor: pointer;
    border-bottom: 1px solid var(--divider-color);
  }
  .ffl-rows .ffl-row:last-child { border-bottom: none; }
  .ffl-row:hover, .ffl-row:focus-visible { background: var(--secondary-background-color); outline: none; }
  .ffl-single .ffl-row { padding: 12px 6px; }

  .ffl-team { min-width: 0; }
  .ffl-team-start { text-align: start; }
  .ffl-team-end { text-align: end; }
  .ffl-team-name {
    font-weight: 500; color: var(--primary-text-color);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .ffl-team.ffl-leader .ffl-team-name { font-weight: 700; }
  .ffl-team-proj { font-size: 0.72rem; color: var(--secondary-text-color); }

  .ffl-scores { display: flex; align-items: baseline; gap: 6px; font-variant-numeric: tabular-nums; }
  .ffl-score { font-size: 1.15rem; color: var(--secondary-text-color); }
  .ffl-score.ffl-leader { color: var(--primary-text-color); font-weight: 700; }
  .ffl-vs { color: var(--secondary-text-color); font-size: 0.8rem; }

  .ffl-stale { font-size: 0.75rem; color: var(--warning-color); padding: 4px 6px; }

  .ffl-banner {
    display: flex; align-items: center; gap: 8px; cursor: pointer;
    margin: 8px -16px 0; padding: 8px 16px;
    background: var(--secondary-background-color);
    border-top: 1px solid var(--divider-color);
    font-size: 0.85rem;
  }
  .ffl-banner:hover, .ffl-banner:focus-visible { filter: brightness(1.05); outline: none; }
  .ffl-banner-label {
    font-size: 0.68rem; text-transform: uppercase; letter-spacing: .04em;
    color: var(--secondary-text-color); flex: 0 0 auto;
  }
  .ffl-banner-text {
    flex: 1 1 auto; min-width: 0; color: var(--primary-text-color);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .ffl-banner-delta { flex: 0 0 auto; font-weight: 700; color: var(--success-color); font-variant-numeric: tabular-nums; }
  .ffl-banner.ffl-correction .ffl-banner-delta { color: var(--error-color); }
  .ffl-banner-empty .ffl-banner-text { color: var(--secondary-text-color); }

  /* Overlay */
  .ffl-overlay { position: fixed; inset: 0; z-index: 9999; display: flex; align-items: center; justify-content: center; }
  .ffl-overlay-backdrop { position: absolute; inset: 0; background: rgba(0,0,0,.55); }
  .ffl-overlay-panel {
    position: relative; background: var(--card-background-color, #fff);
    color: var(--primary-text-color); border-radius: 12px;
    width: min(760px, 94vw); max-height: 88vh; display: flex; flex-direction: column;
    box-shadow: 0 8px 32px rgba(0,0,0,.4); outline: none;
  }
  .ffl-overlay-head {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 12px 16px; border-bottom: 1px solid var(--divider-color);
  }
  .ffl-overlay-title { font-weight: 600; }
  .ffl-overlay-close {
    background: none; border: none; font-size: 1.5rem; line-height: 1; cursor: pointer;
    color: var(--secondary-text-color); padding: 0 4px;
  }
  .ffl-overlay-body { overflow: auto; padding: 12px 16px 16px; }
  .ffl-loading, .ffl-empty { color: var(--secondary-text-color); padding: 12px 0; }
  .ffl-error { color: var(--error-color); padding: 12px 0; }

  .ffl-rosters { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 640px) { .ffl-rosters { grid-template-columns: 1fr; } }
  .ffl-roster-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; }
  .ffl-roster-name { font-weight: 600; }
  .ffl-roster-pts { font-weight: 700; font-variant-numeric: tabular-nums; }
  .ffl-table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
  .ffl-table th {
    text-align: start; font-weight: 500; color: var(--secondary-text-color);
    font-size: 0.7rem; text-transform: uppercase; padding: 2px 4px;
  }
  .ffl-table td { padding: 4px; border-top: 1px solid var(--divider-color); vertical-align: top; }
  .ffl-slot { color: var(--secondary-text-color); white-space: nowrap; }
  .ffl-statline { font-size: 0.7rem; color: var(--secondary-text-color); }
  .ffl-game { color: var(--secondary-text-color); font-size: 0.7rem; white-space: nowrap; }
  .ffl-proj, .ffl-pts { text-align: end; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .ffl-pts { font-weight: 600; }
  .ffl-p-pre .ffl-pname, .ffl-p-pre .ffl-pts { opacity: .65; }
  .ffl-p-live .ffl-pts { color: var(--success-color); }
  .ffl-bench summary { cursor: pointer; font-size: 0.75rem; color: var(--secondary-text-color); padding: 6px 4px; }

  .ffl-history { list-style: none; margin: 0; padding: 0; }
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

  console.info(
    `%c YAHOO-FANTASY-FOOTBALL-CARDS %c ${CARD_VERSION} `,
    "color:white;background:#6001d2",
    ""
  );
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
    renderBanner,
    renderRosterSide,
    renderPlayerRow,
    renderHistory,
    findFflEntity,
  };
}
