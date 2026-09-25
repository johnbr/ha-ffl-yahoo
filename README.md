# Yahoo Fantasy Football for Home Assistant

Live Yahoo Fantasy Football league scoreboards in Home Assistant, with two bundled Lovelace cards.

> **Status: working.** Running live against a real league since 2026-09-09. The data source, the
> coordinator, the scoring-play engine and both cards are in place.

## Features

- **Two cards.** One for your own matchup, one for every matchup in the league. Both show only the
  scores at rest; clicking a matchup opens a roster popup with each player's live points.
- **Live team scores and projections.** Yahoo publishes a live projected total per team that moves
  during games — both cards show current points alongside the projection.
- **Per-matchup scoring plays.** Under each matchup, the play that last moved *that* score,
  aligned to the side of the team it scored for. Bench players are excluded, since their points
  do not count. Clicking the line opens that matchup's full scoring history.
- **Automation hooks.** Each scoring play fires on the Home Assistant event bus, and the options
  flow lets you attach any action sequence to "my player scored", "opponent scored" and
  "lead change".

## How it gets the data

It reads Yahoo's **GameChannel** tier — the same anonymous endpoints
`sports.yahoo.com/nfl/gamechannel/` runs on. **No Yahoo account, no OAuth, no API key, and no
approval process.** Private leagues work; so do public ones.

- `pub-api.fantasysports.yahoo.com/fantasy/v3/redzone/nfl?league_id=…` — teams, rosters, matchups,
  per-player projections, and your league's own scoring modifiers.
- `relay-stream.sports.yahoo.com/nfl/{games,stats}.txt` — the live tier, refreshed every few seconds.

One refresh is **three requests for the whole league**, whatever its size.

### Worth knowing

- **The points are computed, not read.** Yahoo publishes no live fantasy totals on this tier —
  `pfWeek` stays null while games are in progress, and its own browser multiplies each player's live
  stat line by the league's scoring modifiers. So does this. Verified to the cent against Yahoo's
  StatTracker display.
- **Scoring plays are synthesized by diffing consecutive polls,** which is exact to your league's
  settings and catches yardage and reception points, not just touchdowns. Because the live feed
  carries real stat lines, each play is described the way Yahoo describes it —
  `D. Maye 1 Comp, 13 Pass Yds` — rather than as a bare point delta.
- **"Real time" means 30–60 seconds.** That is how far Yahoo's own live scoring trails the play. No
  integration can beat it.
- **Stat corrections happen.** Yahoo revises stats during and after games; these are flagged as
  corrections and never presented as scores.
- **One play lands in pieces.** Yahoo's stat feed moves a category at a time, a poll apart: a
  19-yard catch arrives as `1 Rec`, then `19 Rec Yds`, then a re-measured yard. The pieces are folded
  into one row per play, so the history reads one catch for +2.90 rather than three lines of the same
  sentence.
- **The relay forgets finished games overnight.** It restarts each morning with the schedule only,
  so last night's game comes back listed as scheduled, 0-0. Its line-score rows survive the restart,
  and a game listed as scheduled with a full line score is treated as final with that score — so the
  NFL card files it under "final" and the players who played are not projected for their day twice.
- **Real play-by-play captions the events.** `relay-stream.sports.yahoo.com/nfl/plays-<id>.txt`
  carries full play text with Yahoo player ids inline; it is what the history and the NFL card's
  play list show, matched to each scoring event by player id. A capture is in `tests/fixtures/`.
- **A name is only shortened while it still names somebody.** The NFL card's play list runs on
  `B. Robinson` rather than `Bijan Robinson` to fit a phone. Atlanta played *Bijan* and *Brian*
  Robinson in the same game, so both Robinsons keep their full names there — the check is per game,
  which is what stops a Sunday's worth of common surnames expanding along with them.
- **A dropped live feed holds its last reading rather than scoring zero.** Every point is computed
  from the stat feed, so one failed fetch of it used to read as the whole league losing their games
  at once, and the next poll as everyone scoring them all back in a single play. The previous
  reading stands in for up to 15 minutes; past that the refresh fails instead of freezing points
  under a running clock.

## Installation

### HACS

1. HACS → Integrations → ⋮ → **Custom repositories**
2. Add `https://github.com/johnbr/ha-ffl-yahoo` with category **Integration**
3. Install, then restart Home Assistant
4. Settings → Devices & Services → **Add Integration** → *Yahoo Fantasy Football*

The Lovelace card bundle is served and registered automatically — no manual resource entry and no
file copying.

### Manual

Copy `custom_components/yahoo_fantasy_football/` into your Home Assistant `config/custom_components/`
directory and restart.

## Configuration

You need the numeric league id from your league URL — `802904` in
`https://football.fantasysports.yahoo.com/f1/802904`. Paste either the number or the whole URL. The
second step lists your league's real team names so you can pick your own; skip it and you get the
league scoreboard without the my-matchup sensor.

## Cards

**Entity ids come from the league name,** which is Home Assistant's own rule — a league called
"Kush" produces `sensor.kush_scoreboard` and `sensor.kush_my_team`. Check
Developer Tools → States if you are unsure, or rename them in the UI.

```yaml
type: custom:ffl-league-scoreboard-card
entity: sensor.kush_scoreboard
```

```yaml
type: custom:ffl-my-matchup-card
entity: sensor.kush_my_team
```

## Development

```bash
ruff check .
pytest tests/ -v
node --check custom_components/yahoo_fantasy_football/yahoo-fantasy-football-cards.js
```

Versions are managed by [release-please](https://github.com/googleapis/release-please) from
conventional commits — never bump `manifest.json` by hand.

## Attribution

Fantasy data provided by Yahoo Fantasy. This project is not affiliated with, endorsed by, or
sponsored by Yahoo or the NFL.

## License

[MIT](LICENSE)
