/* Yahoo Fantasy Football cards for Home Assistant. */
const CARD_VERSION = "0.4.0"; // x-release-please-version

const MY_MATCHUP_TAG = "ffl-my-matchup-card";
const LEAGUE_TAG = "ffl-league-scoreboard-card";
const DOCS_URL = "https://github.com/johnbr/ha-ffl-yahoo";
const STYLE_CLASS = "ffl-card-style";

/*
 * Scaffold release. Both cards share this one bundle so they can share the
 * render helpers, the roster/history overlays and the CSS — two custom
 * elements, one Lovelace resource.
 *
 * Deliberate structural choices carried over from the MLB card, so they are in
 * place before the real rendering lands:
 *   - plain HTMLElement + innerHTML, light DOM, zero imports (no build step)
 *   - the stylesheet is injected INSIDE the card element, because Lovelace
 *     nests cards in shadow roots that a document.head sheet cannot reach
 *   - a scalar-only fingerprint guards the innerHTML replacement, so unrelated
 *     state pushes don't thrash the DOM
 */

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]
  );
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
  const match = Object.keys(hass.states).find((id) => id.startsWith("sensor.ffl_") && id.endsWith("_scoreboard"));
  return match || "";
}

/** Shared base: entity plumbing, style injection, fingerprint short-circuit. */
class FflBaseCard extends HTMLElement {
  setConfig(config) {
    this.config = { title: "", ...(config || {}) };
    this._lastFingerprint = "";
  }

  set hass(hass) {
    this._hass = hass;
    if (!this.card) {
      ensureCardStyles(this);
      this.card = document.createElement("ha-card");
      this.card.className = "ffl-card";
      this.content = document.createElement("div");
      this.content.className = "card-content";
      this.card.appendChild(this.content);
      this.appendChild(this.card);
    }
    this.render();
  }

  getCardSize() {
    return 3;
  }

  _stateObj() {
    const entityId = this.config && this.config.entity;
    if (!entityId || !this._hass || !this._hass.states) return null;
    return this._hass.states[entityId] || null;
  }

  render() {
    const st = this._stateObj();
    if (!this.config || !this.config.entity) {
      this._paint(`<div class="ffl-placeholder">Set an <code>entity</code> for this card.</div>`, "no-entity");
      return;
    }
    if (!st) {
      this._paint(
        `<div class="ffl-placeholder">Entity <code>${escapeHtml(this.config.entity)}</code> not found.</div>`,
        "missing"
      );
      return;
    }
    this._renderBody(st);
  }

  _paint(html, fingerprint) {
    if (fingerprint === this._lastFingerprint) return;
    this._lastFingerprint = fingerprint;
    const title = this.config && this.config.title;
    this.content.innerHTML = (title ? `<div class="ffl-title">${escapeHtml(title)}</div>` : "") + html;
  }
}

class FflMyMatchupCard extends FflBaseCard {
  // No getConfigElement yet — HA falls back to the YAML editor, which is
  // correct until the ha-form editor lands with the real card options.
  static getStubConfig(hass) {
    return { entity: findFflEntity(hass) };
  }

  _renderBody(st) {
    const week = st.attributes.week;
    this._paint(
      `<div class="ffl-placeholder">
         <div class="ffl-headline">My matchup</div>
         <div class="ffl-sub">Waiting for league data${week ? ` &middot; week ${escapeHtml(week)}` : ""}.</div>
       </div>`,
      `my|${st.state}|${week ?? ""}`
    );
  }
}

class FflLeagueScoreboardCard extends FflBaseCard {
  static getStubConfig(hass) {
    return { entity: findFflEntity(hass) };
  }

  _renderBody(st) {
    const matchups = Array.isArray(st.attributes.matchups) ? st.attributes.matchups : [];
    const week = st.attributes.week;
    this._paint(
      `<div class="ffl-placeholder">
         <div class="ffl-headline">League scoreboard</div>
         <div class="ffl-sub">${matchups.length} matchups${week ? ` &middot; week ${escapeHtml(week)}` : ""}.</div>
       </div>`,
      `league|${st.state}|${week ?? ""}|${matchups.length}`
    );
  }
}

const CARD_CSS = `
  .ffl-card { overflow: hidden; }
  .ffl-card .card-content { padding: 16px; }
  .ffl-title {
    font-size: 1.1rem;
    font-weight: 600;
    color: var(--primary-text-color);
    margin-bottom: 8px;
  }
  .ffl-placeholder { color: var(--secondary-text-color); font-size: 0.95rem; }
  .ffl-headline { color: var(--primary-text-color); font-weight: 600; margin-bottom: 2px; }
  .ffl-sub { font-size: 0.85rem; }
  .ffl-placeholder code {
    background: var(--secondary-background-color);
    border-radius: 4px;
    padding: 1px 4px;
  }
`;

customElements.define(MY_MATCHUP_TAG, FflMyMatchupCard);
customElements.define(LEAGUE_TAG, FflLeagueScoreboardCard);

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
