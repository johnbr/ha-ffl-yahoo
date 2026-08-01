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

## Re-capturing

ESPN needs no API key. Any past date works, so fixtures can be refreshed at any
time of year:

```bash
curl -s "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event=401772839" \
  | python3 -c 'import json,sys; [print(repr(p["text"])) for p in json.load(sys.stdin)["scoringPlays"]]'
```
