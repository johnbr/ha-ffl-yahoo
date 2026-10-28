# Yahoo Fantasy Football for Home Assistant

Live Yahoo Fantasy Football league scoreboards in Home Assistant, with four bundled Lovelace cards.

> **Status: working.** Running live against a real league since 2026-09-09. The data source, the
> coordinator, the scoring-play engine and all four cards are in place.

![The league scoreboard card and the NFL games card side by side on a tablet dashboard during the early Sunday games](https://raw.githubusercontent.com/johnbr/ha-ffl-yahoo/main/docs/images/dashboard.png)

## Features

- **Four cards.** One for your own matchup, one for every matchup in the league, and one for the
  week's real NFL games. The two fantasy cards show only the
  team names, each with its season record (`Tesla (0-4)`), and the scores at rest; clicking a matchup
  opens a roster popup with each player's live points. The fourth is just your matchup's score, laid
  out like the Android app's home-screen widget, and tapping it opens the same roster.
- **Live team scores and projections.** Yahoo publishes a live projected total per team that moves
  during games — both cards show current points alongside the projection.
- **Per-matchup scoring plays.** Under each matchup, the play that last moved *that* score,
  aligned to the side of the team it scored for. Bench players are excluded, since their points
  do not count. Clicking the line opens that matchup's full scoring history.
- **A player's season.** Clicking a player in the roster popup swaps it for his season, week by
  week: the opponent (or bye), Yahoo's projection, and the points he scored, with the weeks still to
  come projected against their opponents. The points before this week are computed from ESPN's stat
  lines under your league's own scoring, green above the projection and red below it; this week's
  are the live total.
- **Automation hooks.** With your team picked, an event entity fires "my player scored",
  "opponent scored" and "lead change", which you can pick as triggers in the automation editor.
  Every scoring play in the league also fires on the Home Assistant event bus. See
  [Automations](#automations).

<table>
  <tr>
    <td valign="top" width="50%">
      <img src="https://raw.githubusercontent.com/johnbr/ha-ffl-yahoo/main/docs/images/matchup-lineup.png" width="100%"
           alt="An open matchup: both projections against the pre-game ones, a 94% to 6% win bar, and the two lineups facing each other by slot, with each player's points, projection and game">
      <br><sub><b>Tap a matchup</b> for both lineups, both projections and the win probability.</sub>
    </td>
    <td valign="top" width="50%">
      <img src="https://raw.githubusercontent.com/johnbr/ha-ffl-yahoo/main/docs/images/player-season.png" width="100%"
           alt="Jahmyr Gibbs's season, week by week: opponent, Yahoo's projection and his points, green above the projection and red below">
      <br><sub><b>Tap a player</b> for his season, week by week.</sub>
    </td>
  </tr>
  <tr>
    <td valign="top">
      <img src="https://raw.githubusercontent.com/johnbr/ha-ffl-yahoo/main/docs/images/scoring-plays.png" width="100%"
           alt="A matchup's scoring history: each play with the player it scored for and the points it was worth">
      <br><sub><b>Tap a matchup's play line</b> for its scoring history.</sub>
    </td>
    <td valign="top">
      <img src="https://raw.githubusercontent.com/johnbr/ha-ffl-yahoo/main/docs/images/nfl-play-list.png" width="100%"
           alt="An NFL game opened on the games card: the score, clock, down and distance, field position bar, and every play with its down and time">
      <br><sub><b>Tap an NFL game</b> for its play list.</sub>
    </td>
  </tr>
</table>

## How it gets the data

It reads Yahoo's **GameChannel** tier — the same anonymous endpoints
`sports.yahoo.com/nfl/gamechannel/` runs on. **No Yahoo account, no OAuth, no API key, and no
approval process.** Private leagues work; so do public ones.

- `pub-api.fantasysports.yahoo.com/fantasy/v3/redzone/nfl?league_id=…` — teams, rosters, matchups,
  per-player projections, and your league's own scoring modifiers.
- `relay-stream.sports.yahoo.com/nfl/{games,stats}.txt` — the live tier, refreshed every few seconds.

One refresh is **three requests for the whole league**, whatever its size.

A player's season, opened from the roster popup, needs what that tier does not keep, so it also reads:

- the same league seed with `&week=N`, once per week of the season — each week's projections. A
  finished week's are saved, so they are fetched once. Yahoo projects only the players on a roster
  in your league that week, so a week the player was a free agent shows ESPN's projection instead,
  scored with your league's modifiers and set in italics.
- ESPN's public fantasy API (`lm-api-reads.fantasy.espn.com`, also anonymous) — each player's stat
  line for every week played and ESPN's projected line for every week, and the NFL schedule. Yahoo publishes no per-week fantasy points
  without a login, so a finished week's points are the stat line scored with your league's own
  modifiers. Checked against the integration's own totals for week 5 of 2026: 115 of 115 players to
  the cent, every kicker and defence included. A league that scores something ESPN's lines cannot
  say exactly (a forty-yard-play bonus, or short field goals paid by distance) gets those weeks
  marked `≈`.

The first player opened costs about twenty requests and a few seconds; after that it is cached.

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
  corrections and never presented as scores. A play the feed takes back entirely, such as a catch
  wiped off by a penalty on the same snap, drops out of the last play and the recent plays. The
  scoring history keeps both the play and its correction.
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
- **A play's result shows before its text does.** The games feed moves the down, distance and spot
  several seconds before the play's sentence reaches the play feed. In between, the NFL card reads
  the result off the two snapshots — `Gain of 7, 1st down`, `No gain`, `Turnover, Buf ball`,
  `Det touchdown` — in italics, and re-checks that game's play feed every few seconds until Yahoo's
  sentence lands and replaces it. It cannot name the player or tell a penalty from a gain.
- **A name is only shortened while it still names somebody.** The NFL card's play list runs on
  `B. Robinson` rather than `Bijan Robinson` to fit a phone. Atlanta played *Bijan* and *Brian*
  Robinson in the same game, so both Robinsons keep their full names there — the check is per game,
  which is what stops a Sunday's worth of common surnames expanding along with them.
- **A dropped live feed holds its last reading rather than reading as fact.** Points are computed
  from the stat feed and the slate comes from the games feed, so a failed fetch of either used to
  be taken at face value: an empty stat feed as the whole league losing their games at once, an
  empty games feed as there being no games at all — which blanked the NFL card, marked every
  matchup final, and slowed the poll that would have undone it. Each feed's last reading stands in
  for it — 15 minutes for stats, 2 for games, since a stopped clock gives itself away faster than
  stale points — and past that the refresh fails rather than serving a hole.

## Installation

### HACS

The integration is in the HACS default store, so there is no custom repository to add.

[![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=johnbr&repository=ha-ffl-yahoo&category=integration)

1. In HACS, search for **Yahoo Fantasy Football** and **Download** it (or use the button above)
2. Restart Home Assistant
3. Settings → Devices & Services → **Add Integration** → *Yahoo Fantasy Football*

If your HACS doesn't list it yet (the default store can take a few hours to refresh), add it as a
custom repository instead: HACS → ⋮ → **Custom repositories** → `https://github.com/johnbr/ha-ffl-yahoo`,
type **Integration**, then continue from step 1.

### Manual

Copy `custom_components/yahoo_fantasy_football/` into your Home Assistant `config/custom_components/`
directory and restart.

Either way, the integration serves the card bundle itself and registers it as a dashboard resource —
no manual resource entry and no file copying. (In YAML resource mode it can't register it, and logs
the URL to add instead.)

## Configuration

### League not found

If setup reports that Yahoo has no NFL league with that id this season, check the number
after `/f1/` in your current NFL league's address. Yahoo returns `Unable to retrieve cookie.`
for this case; it does not mean you need to sign in or supply a cookie.


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
entity: sensor.kush_scoreboard
```

The my-score card is that matchup at a glance, after the
[Android app's](https://github.com/johnbr/ffl-yahoo-android) widget: your team over your opponent,
with no heading. Each line has the live projection (green above the pre-game projection, red below)
and the points, with the decimal points lined up. Then, in brackets, comes how many of that team's
starters still have a game to finish, in progress or yet to kick off. The last play goes underneath
for three minutes after it scores, and stays while the card is open.
Once the matchup is over, the projections and the counts drop away and the winner turns gold. Tap the
score for both lineups, or the play for the matchup's scoring history.

<img src="https://raw.githubusercontent.com/johnbr/ha-ffl-yahoo/main/docs/images/my-score-card.png" width="400"
     alt="The my-score card: Kush, 3 live; Tesla on 111.74 points, projected 152.04 in green, over Wallyworld 34 on 58.45, projected 104.98 in red; last play P. Nacua 1 rec, 4 yds">

```yaml
type: custom:ffl-my-score-card
entity: sensor.kush_scoreboard
```

Both find your team through the league's `sensor.kush_my_team`. A `team_id:` in the card config
overrides that.

The NFL games card shows the week's real NFL slate — live scores, the down and distance, and each
game's play list — whether or not anyone in the league has a player in it:

```yaml
type: custom:ffl-nfl-games-card
entity: sensor.kush_nfl_games
```

All four can be added from the dashboard's card picker, which fills in the entity.

## Automations

### The matchup event entity

With a team picked during setup, the league gets an event entity, `event.kush_my_matchup` for a league
called "Kush". It has three event types:

| Event type | Fires when |
|---|---|
| `my_player_scored` | One of your starters gains points |
| `opponent_scored` | One of your opponent's starters gains points |
| `lead_change` | The matchup changes hands — or someone goes ahead from 0-0 |

Bench points, stat corrections and negative plays (a fumble lost) do not fire it. A play that Yahoo
reports in pieces (`1 Rec`, then `19 Rec Yds`) fires once, on its first piece. A tie is not a lead
change: a lead that goes to a tie and back to the same team fires nothing.
After a restart, the first leader seen mid-game is taken as the baseline, since there is no
telling who led before. The event's data (below) is on the entity's attributes.

```yaml
triggers:
  - trigger: state
    entity_id: event.kush_my_matchup
    # Not when the entity comes back after a restart.
    not_from: unavailable
conditions:
  - condition: state
    entity_id: event.kush_my_matchup
    attribute: event_type
    state: lead_change
  - condition: state
    entity_id: event.kush_my_matchup
    attribute: side
    state: mine
actions:
  - action: notify.notify
    data:
      message: >-
        {{ state_attr('event.kush_my_matchup', 'leader') }} lead
        {{ state_attr('event.kush_my_matchup', 'leader_points') }}-{{
        state_attr('event.kush_my_matchup', 'trailer_points') }}
```

### Bus events

The entity is built on two events on the Home Assistant event bus, which you can also trigger on
directly (`trigger: event`):

**`yahoo_fantasy_football_scoring_play`** — every scoring play in the league, bench and corrections
included. A play that arrives in pieces fires once per piece, under the same `event_id` with the
running total. Deduplicate on `event_id` if you only want one.

| Field | |
|---|---|
| `entry_id`, `league_id` | Which league (config entry, and Yahoo's league id) |
| `side` | `mine` or `opponent` for a play in your matchup; `null` otherwise, or with no team picked |
| `event_id` | Stable id for the play |
| `player`, `team_key`, `matchup_id` | Who scored, for which fantasy team, in which matchup (`w<week>.m<n>`) |
| `text` | The play as Yahoo describes it — `D. Maye 1 Comp, 13 Pass Yds` |
| `short_text`, `short_who`, `short_what` | A compact form, and its two halves |
| `stat_delta` | What changed in the stat line, in Yahoo's phrasing — `1 Rec TD`. Unlike `text`, never replaced by play-by-play prose |
| `delta`, `points` | Points this play, and the player's total after it |
| `starter` | `false` for a bench player, whose points do not count |
| `correction` | `true` for a stat correction rather than a play |
| `enriched` | Whether real play-by-play text was matched to it |
| `timestamp` | When it was seen (Unix seconds) |

**`yahoo_fantasy_football_lead_change`** — your matchup changed hands. Fires only with a team picked.

| Field | |
|---|---|
| `entry_id`, `league_id`, `matchup_id` | Which league and matchup |
| `side` | `mine` if you went ahead, `opponent` if they did |
| `leader`, `leader_points` | The team now ahead, and its score |
| `trailer`, `trailer_points` | The team now behind, and its score |
| `my_points`, `opponent_points` | Both scores from your side |

```yaml
triggers:
  - trigger: event
    event_type: yahoo_fantasy_football_scoring_play
    event_data:
      side: mine
conditions:
  - condition: template
    value_template: "{{ trigger.event.data.stat_delta is search('TD') }}"
actions:
  - action: light.turn_on
    target:
      entity_id: light.living_room
    data:
      flash: short
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

Fantasy data provided by Yahoo Fantasy. Past weeks' player stat lines and the NFL schedule come from
ESPN. This project is not affiliated with, endorsed by, or sponsored by Yahoo, ESPN or the NFL.

## License

[MIT](LICENSE)
