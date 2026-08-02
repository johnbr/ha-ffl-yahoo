# Test fixtures

Real captures from upstream APIs. **Do not hand-edit the payloads** — if a shape
changes, re-capture it and note the provenance here. Hand-written fixtures test
what we imagined the API does, which is exactly the thing the parser keeps
getting wrong.

## `espn_scoring_plays.json`

Captured 2026-08-01 from ESPN's public NFL API, 2025 regular season:

```
https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=YYYYMMDD
https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={id}
```

Sampled dates `20250907`, `20250921`, `20251012`, `20251123`, `20251225` — 54
games, 215 scoring plays — then reduced to one real record per distinct text
shape. Each entry keeps only the fields the parser reads (`text`, `type`,
`scoringType`, `team`) plus `event`/`date` for provenance.

### Why these specific plays

The set is chosen for the awkward cases, not for coverage of the common ones:

| Capture | What it defends against |
|---|---|
| `Bijan Robinson 50 Yd pass from Michael Penix Jr. (Younghoe Koo Kick)` | A passing TD credits **three** fantasy scorers — receiver, passer, PAT kicker. Parsers that return one athlete per play silently lose two-thirds of passing-TD enrichment. |
| `Michael Penix Jr. 4 Yd Rush (Younghoe Koo Kick)` | A generational suffix directly abutting the parenthetical. |
| `Ka'imi Fairbairn 51 Yd Field Goal ` | **Trailing whitespace** — ESPN emits it on a large fraction of field-goal texts — plus an apostrophe in the name. |
| `Blocked Kick Recovered by Jordan Davis (PHI) Jordan Davis 61 Yd Touchown Return` | The parenthetical is a **team abbreviation, not a kicker**; a naive "parenthetical means PAT" rule invents an athlete called `PHI`. Also contains ESPN's own typo, `Touchown`, so the parser must not match on that spelling. |
| `... (Chase McLaughlin PAT Failed)` | The kicker is named on a **missed** PAT exactly as on a made one; only the wording differs. |
| `... (Tua Tagovailoa Pass to Julian Hill for Two-Point Conversion)` | A successful two-point conversion adds two more credits on top of the touchdown's. |
| `... (Two-Point Run Conversion Failed)` | A parenthetical naming nobody. |
| `Defensive Holding in Endzone for Safety` | A scoring play with **no athlete at all**. |
| `Kenny Moore II 32 Yd Interception Return (Spencer Shrader Kick)` | Roman-numeral suffix; also the D/ST case, where the fantasy scorer is the unit rather than the named defender. |
| `Jaylin Lane 90 Yd Punt Return (Matt Gay Kick)` | A return TD, which is *not* a defensive TD for scoring purposes. |

### Known gaps

Not yet captured, so not yet tested: kickoff-return touchdowns, a successful
two-point **run** conversion, and a safety that credits a named defender. Add
them when a capture turns one up rather than inventing the string.

## `yahoo_web_matchup_2025_w13.html` / `yahoo_web_scoreboard_2025_w13.html`

Captured 2026-08-01 from Yahoo's **public web tier** — a real public league,
fetched with **no cookies and no account**, which is the whole point: it proves
the anonymous path works and pins the parser to markup Yahoo actually served.

```
https://football.fantasysports.yahoo.com/2025/f1/476807?week=13
https://football.fantasysports.yahoo.com/2025/f1/476807/matchup?week=13&mid1=1
```

Both are **trimmed** from ~950 KB responses to the region the parser reads
(team header through the end of the roster tables / the matchups container).
Trimming is the one hand-edit allowed here, because the discarded part is Yahoo
chrome, ads and analytics. It is also a hazard: the scoreboard fixture was cut
too tightly on the first pass and contained only 6 of 10 teams, which failed as
a *parser* bug until the fixture was re-cut. **If a count-based test fails,
check the fixture's coverage before changing the parser.**

### What these defend against

| Case | What it defends against |
|---|---|
| `Vikings - DEF` / `Lions - DEF` rows | Team defenses are the **only** roster slot with no player link and no `data-ys-playerid`. They were silently dropped on the first pass — the failure mode is missing points, not an exception. |
| The mirrored `#statTable1` layout | Both teams share one table, the right-hand side **reversed** (`[7..10]`). An index slip yields plausible-but-wrong numbers, so the totals row is cross-checked against the sum of starters. |
| The playoff bracket on the scoreboard page | `yfa-matchup` blocks carry **weeks 16/17** scores on a week-13 page. Scanning the page for decimals silently reads the wrong week. |
| The transactions module | Links team names, so an unbounded team-name scan picks up extra "teams". |
| `A.J. Brown`, `Michael Pittman Jr.`, `Kenneth Walker III` | Punctuation and suffixes in the name cell, alongside a "Video Forecast" promo link that must not leak into the parsed name. |

### Known gaps

Everything here is a **completed** week. In-progress rows are unobserved — the
live wording of the game note, whether the projection column updates mid-game,
and whether Gamecast is reachable anonymously are all open. `game_state` returns
`unknown` rather than guessing for exactly this reason. Re-capture during a live
game and add the fixture; see the Gamecast capture protocol in `PLAN.md`.

### Re-capturing

Only works for a league whose privacy setting is **public** — a private league
redirects to `login.yahoo.com`:

```bash
curl -s -A "Mozilla/5.0" \
  "https://football.fantasysports.yahoo.com/2025/f1/476807/matchup?week=13&mid1=1" \
  -o matchup.html
```

## Re-capturing (ESPN)

ESPN needs no API key. Any past date works, so fixtures can be refreshed at any
time of year:

```bash
curl -s "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event=401772839" \
  | python3 -c 'import json,sys; [print(repr(p["text"])) for p in json.load(sys.stdin)["scoringPlays"]]'
```
