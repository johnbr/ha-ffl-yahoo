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

## `yahoo_redzone_2026_w1.json` + `yahoo_relay_*_2026_w1.txt`

Captured 2026-09-09 ~19:00 PT from Yahoo's **GameChannel** tier, anonymously —
no cookies, no account, no OAuth — during the live week-1 opener (NE at SEA):

```
https://pub-api.fantasysports.yahoo.com/fantasy/v3/redzone/nfl?league_id={id}&format=json&player_image_type=17
https://relay-stream.sports.yahoo.com/nfl/stats.txt
https://relay-stream.sports.yahoo.com/nfl/games.txt
https://relay-stream.sports.yahoo.com/nfl/plays-26.txt
```

This is the tier `sports.yahoo.com/nfl/gamechannel/` runs on. The source league
is **private**, which is the point: the HTML tier above cannot read it at all.

### The one hand-edit, and why

The relay `.txt` captures are **verbatim** — they are public NFL data with no
league in them. `yahoo_redzone_2026_w1.json` is verbatim in structure but has
had four identifying fields substituted, because the capture is a real private
league and this repo is public:

| Field | Replaced with |
|---|---|
| `leagues.{id}` key and `id` | `999999` |
| `leagues.*.name` | `Test League` |
| `teams.*.name` | Ten generic names |
| `teams.*.managers.*.nickName` | `manager<N>` |
| `teams.*.imageUrl*` | `example.invalid` (Cloudinary URLs embed an account) |

Nothing else was touched — every roster, projection, scoring modifier, stat and
matchup is exactly as served. `test_yahoo_redzone.py` asserts the substitution
held, so a re-capture that forgets it fails rather than leaking.

### Why the asserted numbers are trustworthy

They were cross-checked against Yahoo's **own StatTracker display** in the
browser at the moment of capture, not derived from the same code that reads
them:

| Assertion | Yahoo showed |
|---|---|
| Drake Maye `12.74` | 12.74 |
| A.J. Brown stat line `3 Rec, 26 Rec Yds` | identical string |
| Seahawks D/ST `9.00` (7 pts allowed band 4.0 + 50 return yds 5.0) | 9.00 |
| Three team totals `5.60` / `10.30` / `3.90` | identical |

If these ever fail, the computed score has stopped matching what Yahoo shows
its own users — which is the only definition of "correct" that matters here.

### What each one defends against

| Capture | What it pins |
|---|---|
| `yahoo_redzone_2026_w1.json` | A **D/ST has `primaryPosition: null`** — detecting a defence by that field scored every defence in the league at zero, silently. `positionType: "DT"` is the marker, and its stats live under an NFL team id, not the synthetic `100000+` fantasy id the roster carries. |
| `yahoo_relay_stats_2026_w1.txt` | A player emits **one row per stat group** (`q` passing, `r` rushing, `w` receiving…), so a QB who ran must accumulate across rows rather than have one row win. Also carries a real shutout (`f\|17`), whose headline stat is the value zero. |
| `yahoo_relay_games_2026_w1.txt` | One live game among sixteen scheduled — proves `pre`/`in` both parse, and that a game is reachable from **either** team's id. |
| `yahoo_relay_plays_26_2026_w1.txt` | Full play-by-play with **player ids inline in the text** (`[40881] passed to [42717]…`), plus a scoring play, drive rows and the last-play row. Not yet consumed — captured because a live week-1 game cannot be re-captured later. |

### Known gaps

No capture yet of: a completed week (does `pfWeek` populate once games go
final?), a bye week, a stat correction large enough to cross a defence band, or
overtime. Add them when a capture turns one up rather than inventing the shape.

## `yahoo_relay_games_2026_w2_reset.txt`

Captured 2026-09-18 ~11:50 PT, verbatim, from the same relay:

```
https://relay-stream.sports.yahoo.com/nfl/games.txt
```

This is the games feed **the morning after** a game. The relay restarts each
morning — the header's `source_sequence_number` is back to `1`, stamped
08:04 PT — and comes back with the schedule only: the previous night's Det at
Buf (`20260917002`) is listed `S`, 0-0, no clock, exactly like the fifteen
games still to come. Its `plays-2.txt` at the same moment still held all 187
plays.

### What it defends against

| Capture | What it pins |
|---|---|
| `g\|20260917002\|8\|2\|S\|0\|0\|\|0\|0\|…` | A **played game listed as scheduled**. Read at face value, the games card filed it under "later this week", and every Det and Buf player read as not yet played — projected for their whole day *on top of* the points they had scored, and counted among the starters still to play. |
| `h\|2\|8\|0\|10\|7\|14` / `h\|2\|2\|14\|13\|7\|7` | The **line-score rows survive the restart**, one cell per period, and sum to the real result (31-41). This is where a forgotten game's final score comes from — see `settle_forgotten_games`. |
| `h\|26\|17\|0\|7\|0` in the week-1 capture | The same row mid-game has one cell per period *played so far*, the current one included — so three cells is a game in progress, not a result. |
