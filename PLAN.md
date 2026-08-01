# Yahoo Fantasy Football scoreboard — new integration + cards

## Context

You want live Yahoo Fantasy Football league tracking in Home Assistant: two Lovelace cards
(your own matchup, and the whole league), each showing just scores at rest, expanding to
rosters on click, with a "last scoring play" banner that opens a scoring-play history.
Layout and behavior should loosely follow the MLB scoreboard cards you already run.

Nothing like this exists in the setup today.

**Current state:** the repo is scaffolded and published so it can enter the HACS default-repository
queue immediately (that review takes months), with development continuing while it waits. Milestone
1 is done — structure, CI, brand assets, a loadable placeholder integration and the card-delivery
pipeline. Milestones 2–6 below are the actual functionality.

---

## Viability — read this first

Researched against Yahoo's Fantasy API, ESPN's public NFL API, and HA's OAuth helpers.
Three things do **not** work as literally described; the rest does.

| Requirement | Verdict |
|---|---|
| Track a specified league's matchups + scores | ✅ `/league/{key}/scoreboard;week=N` |
| Projected **team** totals | ✅ `team_projected_points` — live, updates during games |
| Projected **player** totals | ❌ **Not exposed by Yahoo's API at all.** Team projections only (your call) |
| Per-player live scores | ✅ `/league/{key}/teams/roster/players/stats;type=week;week=N` |
| "All scoring plays … in real time" | ⚠️ **Yahoo has no play-by-play.** Hybrid design below (your call) |
| Two cards, score-only + click-to-expand + banner | ✅ Straight port of the MLB card patterns |

### The three caveats in detail

1. **Yahoo API access is now approval-gated.** Yahoo no longer issues Fantasy keys instantly
   from the developer portal — you submit an application describing your product/use case and
   wait for approval. **This is a hard prerequisite and the only true blocker.** Everything can
   be built and unit-tested against fixtures without it, but it can't run live until you hold a
   Client ID + Secret. Apply early: <https://sports.yahoo.com/developer/access/>.

2. **No play-by-play.** Yahoo returns point *totals*, never events. Approved approach — a two
   layer feed:
   - **Layer A (always on, authoritative):** diff `player_points` between polls. Every change is
     an event: `P. Nacua +6.4 → 18.9`. This is exact to *your* league's scoring settings and
     catches yardage/reception points, not just touchdowns.
   - **Layer B (optional enrichment):** poll ESPN's free NFL API and attach the real play text
     to TD/FG events → `Nacua 24 yd TD pass from Stafford · +6.4 → 18.9`. Verified live today:
     `site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard` (121 KB, whole slate) and
     `…/summary?event={id}` → `scoringPlays[]` with `text`, `type`, `period`, `clock`.
     **Gotcha found during research:** `scoringPlays[]` carries no athlete IDs — only the team and
     a text string — so attribution is surname + NFL-team + time-window matching, which is
     heuristic. It is gated behind a confidence check and degrades silently to Layer A.
   - **Ceiling on "real time":** Yahoo's own live scoring lags the play by roughly 30–60 s. No
     design can beat that. Layer B's play text can arrive first and is held briefly to pair up.

3. **Stat corrections produce negative deltas.** Yahoo revises stats mid-game and for days
   after. These must be rendered as corrections, never as scoring plays.

Two smaller ones: Yahoo's `?format=json` is notoriously awkward (numeric-keyed pseudo-arrays),
so a normalizer module with real fixtures is required, not optional; and Yahoo's rate limits are
undocumented and throttled per app ID, so polling stays conservative with backoff + serve-stale.

---

## Approach

Mirror `~/repo/mlb-live-scoreboard` almost exactly: a single custom integration whose card JS
ships **inside** `custom_components/<domain>/`, is copied to `www/community/<domain>/` at
`async_setup`, and self-registers as a Lovelace resource with a `?v=<version>` cache-buster.
No build step, no npm, zero pip requirements.

The one thing MLB has no precedent for is OAuth. Resolved: HA's `application_credentials` +
`config_entry_oauth2_flow`, which uses `https://my.home-assistant.io/redirect/oauth` as the
redirect URI — satisfying Yahoo's HTTPS requirement **without exposing HA to the internet**.
That exact URL goes in the Yahoo app registration.

### Repo layout

```
~/repo/ha-ffl-yahoo/
├── PLAN.md                     ← the deliverable of this task
├── README.md, LICENSE (MIT), hacs.json, pyproject.toml
├── release-please-config.json, .release-please-manifest.json
├── .pre-commit-config.yaml, .prettierrc.json, .gitignore
├── .github/workflows/{tests,validate,hassfest,release-please}.yml
├── custom_components/yahoo_fantasy_football/
│   ├── __init__.py             card registration + WS commands (port of MLB's)
│   ├── manifest.json           config_flow, dependencies [http, lovelace,
│   │                           application_credentials], requirements []
│   ├── application_credentials.py   Yahoo OAuth2 implementation
│   ├── config_flow.py          OAuth handshake → league picker → options flow
│   ├── api.py                  authed Yahoo client (OAuth2Session + refresh)
│   ├── yahoo_parse.py          pure normalizers for Yahoo's JSON  ← most-tested module
│   ├── espn.py                 optional NFL enrichment client
│   ├── plays.py                point-delta → scoring-event engine + ring buffer
│   ├── coordinator.py          adaptive polling, TTL caches, event dispatch
│   ├── sensor.py + binary_sensor.py
│   ├── const.py, types.py, strings.json, translations/en.json, brand/
│   └── ffl-scoreboard-cards.js  ONE file, TWO custom elements
└── tests/  conftest.py (sys.modules HA stubs), test_*.py, fixtures/*.json
```

Both cards live in one JS file so they share ~1000 lines of render helpers, the popup overlay,
and the CSS — two `customElements.define` + two `window.customCards.push`, one resource entry.

### Backend

**`api.py`** — thin async client over `config_entry_oauth2_flow.OAuth2Session` (auto-refreshes
the 1-hour access token). Two calls per refresh:
- `/league/{league_key}/scoreboard;week={w}?format=json` → matchups, `team_points`,
  `team_projected_points`, `win_probability`
- `/league/{league_key}/teams/roster;week={w}/players/stats;type=week;week={w}?format=json` →
  every team's roster with live player points in **one** request.
  *Verify this chained sub-resource on first live run;* fall back to N per-team calls (~12) if
  Yahoo rejects it. `/users;use_login=1/games;game_keys=nfl/teams` resolves "my team" once.

⚠️ Yahoo may require HTTP Basic auth on the token exchange rather than credentials in the body.
If `LocalOAuth2Implementation` gets `invalid_client`, subclass and override the token request.

**`plays.py`** — the scoring-play engine.
- Diff previous vs current `player_points` per player key. `|Δ| ≥ 0.05` emits an event carrying
  player, NFL team, owning fantasy team, matchup id, delta, new total, timestamp.
- Negative deltas → `correction: true`, styled differently, excluded from the banner.
- Enrichment pairs an event with an ESPN `scoringPlays[]` entry when surname + NFL team match
  and the play is within a short window; otherwise the event keeps its Yahoo-only text.
- `collections.deque(maxlen=200)` per week, persisted through `homeassistant.helpers.storage.Store`
  so a mid-Sunday HA restart doesn't lose the history.
- Fires bus event `yahoo_fantasy_football_scoring_play`; options-flow `ActionSelector` fields
  (`on_my_player_scored`, `on_opponent_player_scored`, `on_lead_change`) run via
  `helpers.script.Script` — same shape as MLB's six event actions in `config_flow.py`.

**`coordinator.py`** — `_compute_update_interval` reassigned every refresh, exactly the MLB
pattern (`coordinator.py:3169`):
- any rostered player's NFL game in progress → **45 s**
- within 30 min of a kickoff / Sunday game window → **5 min**
- otherwise (Tue–Sat) → **30 min**

That's ≤160 Yahoo calls/hour at peak. Every fetch is try-fresh → serve-stale-within-TTL →
degrade, and a failed ESPN call must never fail the refresh.

**Entities** (one config entry per league):
- `sensor.ffl_<league>_scoreboard` — state is a low-churn id (`w<week>-<matchup_count>`);
  attributes hold compact per-matchup summaries + `last_play` + `recent_plays` (last 10).
- `sensor.ffl_<league>_my_team` — state is your live point total; attributes carry your matchup
  and your roster (always fetched anyway, so the popup opens instantly).
- `binary_sensor.ffl_<league>_scoring_active` — for dashboard conditionals. Your repo's
  CLAUDE.md is explicit that a card can't be gated on a sensor *attribute* from the frontend,
  so this exists for the same reason `binary_sensor.world_cup_games_today` does.

**Attribute budget** — every entity sets `_unrecorded_attributes = frozenset({MATCH_ALL})`
(`sensor.py:19` in the MLB repo). Only summaries go in attributes; full rosters and history are
served on demand over WebSocket, read straight from coordinator memory (no upstream call):
- `yahoo_fantasy_football/matchup_detail` — both rosters for one matchup
- `yahoo_fantasy_football/play_history` — the ring buffer, optionally filtered to a matchup
- `yahoo_fantasy_football/week_at_offset` — prior/next week (port of MLB's `game_at_offset`)

Registration guarded by `hass.data[DOMAIN]["_ws_registered"]`, with `voluptuous`/`websocket_api`
imported *inside* the register functions so the stubbed test harness doesn't need them
(`__init__.py:120`).

### Cards

`custom:ffl-my-matchup-card` (one matchup) and `custom:ffl-league-scoreboard-card` (all of them).
Both are plain `HTMLElement` + `innerHTML`, light DOM, no imports — ported from
`mlb-live-game-card.js`. Reused verbatim from that file:

- `ensureCardStyles(host)` injecting `<style>` **inside** the card element — a document.head
  sheet can't reach content nested in Lovelace's shadow roots (that was a real bug fix there)
- scalar-only render fingerprint short-circuit (`_computeRenderFingerprint`, line 3084) — never
  `JSON.stringify` over play arrays
- the blob-URL logo cache (`window.__mlbLiveLogoCache`, line 319) — `innerHTML` replacement
  destroys every `<img>` each render
- delegated `click`/`keydown` listeners attached once to `this.content`
- `<ha-form>`-based editor via `getConfigElement` (line 261) — needs no build step
- `window.customCards.push({type, name, description, preview: true, documentationURL})`

**Matchup row** — team logo + name + manager on the left; live points and projected total on the
right, winner highlighted. That's all that shows at rest, per your spec.

**Roster popup on click** — hand-rolled `role="dialog"` overlay appended to `document.body`
(MLB's `_ensurePlayerCardPopup`, line 2118), *not* `<ha-dialog>` and **not browser_mod**: zero
imports, works in every HA context, and your CLAUDE.md already documents browser_mod's
`autoclose` as a hover-misfeature to avoid. Backdrop + ✕ + ESC close, focus trap, scroll lock,
loading/error/empty/ready states, and the `_pcToken` race guard so a slow response can't
overwrite a newer one. Body: both rosters side by side — position, player, NFL matchup, game
status (yet to play / in progress / final), live points, with bench collapsed by default.

**Banner** — a full-width strip beneath the card showing the most recent scoring play (league-wide
on the league card; matchup-scoped on the my-matchup card). Clicking it opens a second overlay
with the reverse-chronological history for the week, filterable to your team. Corrections render
muted and are excluded from the banner itself.

---

## Milestones

1. **Scaffold + repo** — `gh repo create johnbr/ha-ffl-yahoo --public --clone`, write `PLAN.md`,
   copy the house scaffolding (hacs.json, pyproject.toml, release-please config, four CI
   workflows, pre-commit) from `~/repo/mlb-live-scoreboard`. Leave uncommitted.
2. **OAuth + client** — `application_credentials.py`, config flow, `api.py`, league picker.
   First live end-to-end call. *Gated on Yahoo approval.*
3. **Normalizer + fixtures** — `yahoo_parse.py` with captured JSON fixtures and pytest coverage.
   This is where Yahoo's JSON weirdness gets contained; build it before the coordinator.
4. **Coordinator + entities** — adaptive polling, two sensors, one binary_sensor, WS commands.
5. **Cards** — score rows → roster popup → banner → history overlay, plus the `ha-form` editor.
6. **Scoring-play engine + ESPN enrichment** — `plays.py`, then `espn.py` behind an option.
   Last because it's the most speculative and the cards work without it.

---

## Verification

- **Unit** — `pytest tests/` with `conftest.py` stubbing `homeassistant.*` in `sys.modules`
  (copy `mlb-live-scoreboard/tests/conftest.py`, 141 lines) so CI needs no HA install. Cover
  `yahoo_parse` against real captured payloads and `plays.py` against a scripted sequence of
  poll snapshots — including a stat correction and a same-player double score.
- **Lint** — `ruff check .`, `python3 -m py_compile custom_components/**/*.py`,
  `node --check custom_components/yahoo_fantasy_football/ffl-scoreboard-cards.js`.
- **Replay harness** — a script that feeds recorded poll snapshots through the coordinator so the
  full card can be exercised mid-week with no live games. Essential: today is 2026-08-01,
  preseason starts ~Aug 7 and the regular season ~Sep 10.
- **Live on norm** — copy `custom_components/yahoo_fantasy_football/` to
  `/etc/homeassistant/custom_components/`, restart HA, add the integration in the UI. The card
  self-registers as a Lovelace resource; confirm via Settings → Dashboards → Resources that the
  `?v=` matches the manifest version, and hard-refresh the browser.
- **Dashboard** — add both cards to `scratch_pad.yaml` first, then fold into `sports.yaml`
  alongside the existing World Cup and MLB cards once stable.
- **Rate-limit sanity** — after a first live Sunday, check the HA log for Yahoo 429/999
  responses and confirm the interval actually stepped 30 min → 5 min → 45 s.

## Out of scope

Draft tools, waiver/trade management, historical season analytics, and multi-sport support
(the API is generic but NFL scoring/positions are hardcoded in v1).
