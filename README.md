# Yahoo Fantasy Football for Home Assistant

Live Yahoo Fantasy Football league scoreboards in Home Assistant, with two bundled Lovelace cards.

> **Status: early scaffold (v0.1.x).** The repository structure, CI and card-delivery pipeline are in
> place; the Yahoo API client, coordinator and scoring-play engine are under active development.
> Installing it today gives you a placeholder entity and two placeholder cards. See
> [PLAN.md](PLAN.md) for the full design and milestone list.

## Planned features

- **Two cards.** One for your own matchup, one for every matchup in the league. Both show only the
  scores at rest; clicking a matchup opens a roster popup with each player's live points.
- **Live team scores and projections.** Yahoo publishes a live projected total per team that moves
  during games — both cards show current points alongside the projection.
- **Scoring-play banner.** A strip beneath each card showing the most recent scoring play, which
  opens the full week's scoring history when clicked.
- **Automation hooks.** Each scoring play fires on the Home Assistant event bus, and the options
  flow lets you attach any action sequence to "my player scored", "opponent scored" and
  "lead change".

## Requirements and limitations

Worth knowing before you invest time in this:

- **Yahoo API access is approval-gated.** Yahoo no longer issues Fantasy Sports API credentials
  instantly — you apply at [sports.yahoo.com/developer/access](https://sports.yahoo.com/developer/access/)
  and wait for approval. You will need your own Client ID and Secret.
- **Yahoo has no play-by-play data.** The API exposes point *totals*, never events. Scoring plays are
  synthesized by diffing player point totals between polls, which is exact to your league's scoring
  settings and catches yardage and reception points, not just touchdowns. Optionally, real play
  descriptions are matched in from ESPN's public NFL API.
- **"Real time" means 30–60 seconds.** That is how far Yahoo's own live scoring trails the play. No
  integration can beat it.
- **Per-player projections are not available.** Yahoo exposes projected points per *team* only.
- **Stat corrections happen.** Yahoo revises stats during and after games; these show as corrections
  rather than as scoring plays.

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

You will need your Yahoo league key. It is derived from your league URL — `https://football.fantasysports.yahoo.com/f1/123456`
is league key `nfl.l.123456`.

## Cards

```yaml
type: custom:ffl-my-matchup-card
entity: sensor.ffl_nfl_l_123456_scoreboard
```

```yaml
type: custom:ffl-league-scoreboard-card
entity: sensor.ffl_nfl_l_123456_scoreboard
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
