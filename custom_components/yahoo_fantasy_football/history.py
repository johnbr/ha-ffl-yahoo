"""A player's season, week by week: Yahoo's projection, what he scored, and whom he played.

No single anonymous source has all three, so this joins them:

* **Yahoo's league seed for each week** — the same ``redzone`` payload the poll reads, asked for
  another week with ``&week=N``. A past week comes back with that week's rosters and each rostered
  player's projection for it (a player ruled out reads 0.00, so it is the final, pre-kickoff number);
  a future week with today's rosters and that week's projections. Only players on a roster in this
  league that week have one.
* **ESPN's fantasy API** — every player's raw stat line for each week played. Yahoo publishes no
  per-week fantasy points without a login, so the points are computed here, from those lines and
  this league's own scoring modifiers, by the same :func:`yahoo_redzone.score` the live totals use.
  Checked against the integration's own week-5 totals: 115 of 115 to the cent, every kicker and
  every defence included (``tests/fixtures/espn_week5_vs_yahoo.json``).
* **ESPN's NFL schedule** — each club's opponent every week, and its bye. ESPN numbers the clubs
  exactly as Yahoo does (New England is 17 on both), so no table joins them.

Everything here is pure: payloads in, rows out. Fetching and caching live in
:mod:`history_client`.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any

from .yahoo_redzone import _defense_stats, redzone_url, score, team_abbr

ESPN_READS = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons"


def seed_week_url(league_id: str | int, week: int) -> str:
    """The league seed as it stood, or will stand, in ``week``."""
    return f"{redzone_url(league_id)}&week={int(week)}"


def espn_players_url(season: int) -> str:
    """Every active NFL player ESPN knows: id, name, club and position. About 650 KB."""
    return f"{ESPN_READS}/{season}/players?scoringPeriodId=0&view=players_wl"


ESPN_PLAYERS_FILTER = json.dumps({"filterActive": {"value": True}})


def espn_stats_url(season: int) -> str:
    """One player's weekly stat lines; :func:`espn_stats_filter` says which player."""
    return f"{ESPN_READS}/{season}/segments/0/leaguedefaults/3?view=kona_player_info"


def espn_stats_filter(espn_id: int) -> str:
    """The ``X-Fantasy-Filter`` header for one player's actual (not projected) weekly lines."""
    return json.dumps(
        {
            "players": {
                "filterIds": {"value": [int(espn_id)]},
                "filterStatsForSourceIds": {"value": [0]},
                "filterStatsForSplitTypeIds": {"value": [1]},
            }
        }
    )


def espn_schedule_url(season: int) -> str:
    """Every club's games, week by week, and its bye. About 110 KB."""
    return f"{ESPN_READS}/{season}?view=proTeamSchedules_wl"


def season_of(week_info: dict[str, Any]) -> int | None:
    """The NFL season a ``weekInfo`` belongs to: a January or February week is last year's."""
    try:
        start = date.fromisoformat(str(week_info["start"]))
    except (KeyError, TypeError, ValueError):
        return None
    return start.year - 1 if start.month < 3 else start.year


# ---------------------------------------------------------------------------
# ESPN's players, matched to Yahoo's
# ---------------------------------------------------------------------------

# ESPN's ``defaultPositionId`` for the positions a fantasy roster holds.
ESPN_POSITIONS: dict[str, int] = {"QB": 1, "RB": 2, "WR": 3, "TE": 4, "K": 5, "DEF": 16}
ESPN_DEFENSE = ESPN_POSITIONS["DEF"]


@dataclass(frozen=True)
class EspnPlayer:
    espn_id: int
    name: str
    team_id: str
    position_id: int


def parse_espn_players(payload: Any) -> list[EspnPlayer]:
    players = []
    for entry in payload if isinstance(payload, list) else []:
        try:
            players.append(
                EspnPlayer(
                    espn_id=int(entry["id"]),
                    name=str(entry.get("fullName") or ""),
                    team_id=str(entry.get("proTeamId") or 0),
                    position_id=int(entry.get("defaultPositionId") or 0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return players


_SUFFIX = re.compile(r"\b(?:jr|sr|ii|iii|iv|v)\b\.?")


def name_key(name: str) -> str:
    """``"Kenneth Walker III"`` and ``"Kenneth Walker"`` alike: letters only, suffixes gone."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]", "", _SUFFIX.sub("", ascii_name))


def _surname_key(name: str) -> str:
    words = [w for w in _SUFFIX.sub("", name.lower()).split() if w]
    return name_key(words[-1]) if words else ""


def match_espn_player(
    players: list[EspnPlayer], name: str, team_id: str, position: str
) -> EspnPlayer | None:
    """ESPN's entry for a Yahoo player, or None rather than a guess.

    A defence is its club. Anyone else is matched on name, then position, then club: ESPN lists
    two Lamar Jacksons (Baltimore's quarterback and a free-agent corner) and two Justin Jeffersons
    (a Minnesota receiver and a Cleveland linebacker). A name written differently on each side —
    "Cam" for "Cameron" — falls back to the one player at that club and position with that surname.
    """
    position_id = ESPN_POSITIONS.get(position.upper())
    if position_id == ESPN_DEFENSE:
        return next(
            (p for p in players if p.position_id == ESPN_DEFENSE and p.team_id == str(team_id)),
            None,
        )
    key = name_key(name)
    named = [p for p in players if name_key(p.name) == key]
    for narrow in (
        lambda p: position_id is None or p.position_id == position_id,
        lambda p: p.team_id == str(team_id),
    ):
        if len(named) > 1:
            named = [p for p in named if narrow(p)] or named
    if len(named) == 1:
        return named[0]
    if named:
        return None
    surname = _surname_key(name)
    alike = [
        p
        for p in players
        if p.team_id == str(team_id)
        and p.position_id == position_id
        and _surname_key(p.name) == surname
    ]
    return alike[0] if len(alike) == 1 else None


def parse_espn_weeks(payload: Any, espn_id: int, season: int) -> dict[int, dict[str, float]]:
    """``{week: ESPN's raw stat line}`` for the weeks this player has one.

    A week with no line is a bye, or a game he did not record a stat in.
    """
    weeks: dict[int, dict[str, float]] = {}
    for entry in (payload or {}).get("players") or []:
        player = entry.get("player") or {}
        if int(player.get("id") or entry.get("id") or 0) != int(espn_id):
            continue
        for line in player.get("stats") or []:
            if (
                line.get("seasonId") != season
                or line.get("statSourceId") != 0
                or line.get("statSplitTypeId") != 1
            ):
                continue
            week = int(line.get("scoringPeriodId") or 0)
            if week > 0:
                weeks[week] = {str(k): float(v) for k, v in (line.get("stats") or {}).items()}
    return weeks


# ---------------------------------------------------------------------------
# ESPN's stat line, in Yahoo's terms
# ---------------------------------------------------------------------------

# Yahoo's stat name -> the ESPN stat ids that sum to it. ESPN's ids are its fantasy API's own
# (cwendt94/espn-api documents them). The names are the relay's, so the league's modifiers
# apply to these exactly as they do to the live lines.
_OFFENSE: dict[str, tuple[int, ...]] = {
    "passingAttempts": (0,), "completions": (1,), "incompletePasses": (2,),
    "passingYards": (3,), "passingTDs": (4,), "passingInterceptions": (20,), "sacked": (64,),
    "rushingAttempts": (23,), "rushingYards": (24,), "rushingTouchdowns": (25,),
    "receptions": (53,), "receptionYards": (42,), "receptionTDs": (43,),
    "receivingTargets": (58,),
    "returnYards": (114, 115), "returnTDs": (101, 102),
    "twoPointConversions": (19, 26, 44),
    "fumbles": (68,), "fumblesLost": (72,), "offensiveFumbleReturnTDs": (63,),
    "fieldGoalsMade40through49": (77,), "fieldGoalsMissed40through49": (79,),
    "fieldGoalsMade50plus": (74,), "fieldGoalsMissed50plus": (76,),
    "fieldGoalsMade60plus": (201,), "fieldGoalsMissed60plus": (203,),
    "fieldGoalsMade": (83,), "fieldGoalsMissed": (85,),
    "patMade": (86,), "patMissed": (88,),
}

_DEFENSE: dict[str, tuple[int, ...]] = {
    "pointsAllowed": (120,), "yardsAllowed": (127,),
    "sacks": (99,), "interceptions": (95,), "fumblesRecovered": (96,),
    # Interception, fumble and blocked-kick returns: the defence's own touchdowns. Kick and punt
    # returns are the special teams' — ``specialTeamsReturnTDs`` — and are not counted twice.
    "defensiveTDs": (103, 104, 93),
    "intRetTds": (103,), "fumbleRecTds": (104,), "blkFgRetTds": (93,),
    "safeties": (98,), "blockedKicks": (97,),
    "specialTeamsReturnYards": (114, 115), "kickReturnYards": (114,), "puntReturnYards": (115,),
    "specialTeamsReturnTDs": (101, 102), "kickRetTds": (101,), "puntRetTds": (102,),
    "extraPointReturned": (205,),
}

# ESPN counts field goals under 40 yards as one bucket; Yahoo has three.
_UNDER_40_MADE = ("fieldGoalsMade0through19", "fieldGoalsMade20through29", "fieldGoalsMade30through39")
_UNDER_40_MISSED = (
    "fieldGoalsMissed0through19", "fieldGoalsMissed20through29", "fieldGoalsMissed30through39",
)
_ESPN_UNDER_40_MADE, _ESPN_UNDER_40_MISSED = 80, 82


def _sum(espn: dict[str, float], ids: tuple[int, ...]) -> float:
    return sum(espn.get(str(i), 0.0) for i in ids)


def yahoo_stats(espn: dict[str, float], defense: bool) -> dict[str, float]:
    """ESPN's raw line for one week, as the stat names the league's modifiers are keyed by.

    A defence also gets the bands it is scored on ("7-13 points allowed"), derived by the same
    code that derives them from the live feed.
    """
    if defense:
        raw = {name: _sum(espn, ids) for name, ids in _DEFENSE.items()}
        raw["gamesPlayed"] = 1.0
        return _defense_stats(raw)
    stats = {name: _sum(espn, ids) for name, ids in _OFFENSE.items()}
    # 50+ less 60+ is 50-59, for the leagues that score the two apart.
    stats["fieldGoalsMade50through59"] = stats["fieldGoalsMade50plus"] - stats["fieldGoalsMade60plus"]
    stats["fieldGoalsMissed50through59"] = (
        stats["fieldGoalsMissed50plus"] - stats["fieldGoalsMissed60plus"]
    )
    # The under-40 kicks go in the 30-39 bucket. Exact when the league pays the three alike, as most
    # do; see unresolved_stats for when it does not.
    for name in (*_UNDER_40_MADE, *_UNDER_40_MISSED):
        stats[name] = 0.0
    stats["fieldGoalsMade30through39"] = espn.get(str(_ESPN_UNDER_40_MADE), 0.0)
    stats["fieldGoalsMissed30through39"] = espn.get(str(_ESPN_UNDER_40_MISSED), 0.0)
    return stats


def _resolvable() -> frozenset[str]:
    offense = yahoo_stats({}, defense=False)
    defense = yahoo_stats({}, defense=True)
    return frozenset(offense) | frozenset(defense)


_RESOLVABLE = _resolvable()


def unresolved_stats(modifiers: dict[str, float]) -> frozenset[str]:
    """Stats this league scores that ESPN's lines cannot say exactly.

    Empty for most leagues, Kush included. Two ways in: a stat ESPN does not report at all (a
    forty-yard-play bonus, say), or under-40 field goals in a league that pays 0-19, 20-29 and
    30-39 differently.
    """
    scored = {name for name, modifier in modifiers.items() if modifier}
    missing = {name for name in scored if name not in _RESOLVABLE}
    for bucket in (_UNDER_40_MADE, _UNDER_40_MISSED):
        if len({modifiers.get(name, 0.0) for name in bucket}) > 1:
            missing.update(name for name in bucket if name in scored)
    return frozenset(missing)


def week_points(
    espn: dict[str, float], defense: bool, modifiers: dict[str, float], unresolved: frozenset[str]
) -> tuple[float, bool]:
    """``(points, exact)`` for one ESPN line under this league's scoring."""
    points = score(yahoo_stats(espn, defense), modifiers)
    if not unresolved:
        return points, True
    under_40 = set(_UNDER_40_MADE) | set(_UNDER_40_MISSED)
    if unresolved <= under_40:
        # Only the field-goal buckets are in doubt, and only for a week with a short kick in it.
        kicks = espn.get(str(_ESPN_UNDER_40_MADE), 0.0) + espn.get(str(_ESPN_UNDER_40_MISSED), 0.0)
        return points, kicks == 0
    return points, False


# ---------------------------------------------------------------------------
# The schedule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fixture:
    week: int
    opponent_id: str
    home: bool
    kickoff: float | None


def parse_espn_schedule(payload: Any) -> dict[str, dict[int, Fixture]]:
    """``{club id: {week: fixture}}``. A week a club has no fixture in is its bye."""
    clubs: dict[str, dict[int, Fixture]] = {}
    for team in ((payload or {}).get("settings") or {}).get("proTeams") or []:
        club = str(team.get("id") or 0)
        if club == "0":
            continue  # ESPN's "free agent" club
        weeks: dict[int, Fixture] = {}
        for week, games in (team.get("proGamesByScoringPeriod") or {}).items():
            for game in games or []:
                home = str(game.get("homeProTeamId")) == club
                opponent = game.get("awayProTeamId") if home else game.get("homeProTeamId")
                kickoff = game.get("date")
                weeks[int(week)] = Fixture(
                    week=int(week),
                    opponent_id=str(opponent),
                    home=home,
                    kickoff=float(kickoff) / 1000.0 if kickoff else None,
                )
        clubs[club] = weeks
    return clubs


def season_weeks(schedule: dict[str, dict[int, Fixture]]) -> list[int]:
    """Every week anyone plays in — the regular season, bye weeks and all."""
    return sorted({week for weeks in schedule.values() for week in weeks})


# ---------------------------------------------------------------------------
# Yahoo's side: who the player is, and what each week projected him for
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    player_id: str
    name: str
    team_id: str
    position: str

    @property
    def defense(self) -> bool:
        return self.position == "DEF"


def _league(seed: dict[str, Any], league_id: str | int) -> dict[str, Any]:
    service = (seed or {}).get("service") or {}
    return ((service.get("leagues") or {}).get(str(league_id))) or {}


def player_identity(seed: dict[str, Any], league_id: str | int, player_id: str) -> Identity | None:
    """The seed's own record of a rostered player — Yahoo's name, club id and position."""
    service = (seed or {}).get("service") or {}
    meta = (service.get("players") or {}).get(str(player_id))
    if not meta:
        return None
    defense = str(meta.get("positionType") or "").upper() == "DT"
    position = "DEF" if defense else str(meta.get("position") or "").split(",")[0].upper()
    return Identity(
        player_id=str(player_id),
        name=str(meta.get("name") or ""),
        team_id=str(meta.get("teamId") or ""),
        position=position,
    )


def week_projections(seed: dict[str, Any], league_id: str | int) -> dict[str, float]:
    """``{player id: projection}`` for every player on a roster in the seed's week."""
    projections: dict[str, float] = {}
    for team in (_league(seed, league_id).get("teams") or {}).values():
        for entry in team.get("players") or []:
            value = entry.get("projectedPoints")
            if value in (None, ""):
                continue
            try:
                projections[str(entry.get("id"))] = float(value)
            except (TypeError, ValueError):
                continue
    return projections


def seed_week(seed: dict[str, Any], league_id: str | int) -> int:
    return int((_league(seed, league_id).get("weekInfo") or {}).get("week") or 0)


def seed_week_info(seed: dict[str, Any], league_id: str | int) -> dict[str, Any]:
    return _league(seed, league_id).get("weekInfo") or {}


# ---------------------------------------------------------------------------
# The season
# ---------------------------------------------------------------------------


def season_rows(
    identity: Identity,
    weeks: list[int],
    current_week: int,
    projections: dict[int, dict[str, float]],
    actuals: dict[int, dict[str, float]] | None,
    fixtures: dict[int, Fixture],
    modifiers: dict[str, float],
    live_points: float | None,
) -> list[dict[str, Any]]:
    """One row per week of the season, in order.

    ``status`` is ``final`` before this week, ``live`` for it, ``upcoming`` after it, and ``bye``
    for the club's bye whenever it falls. A finished week's ``points`` are ESPN's line under this
    league's scoring — 0.00 when the club played and he recorded nothing — or ``None`` when ESPN
    has no player to match; this week's are the live total the cards already show. ``projected``
    is ``None`` for a week he was on no roster in this league.
    """
    unresolved = unresolved_stats(modifiers)
    rows = []
    for week in weeks:
        fixture = fixtures.get(week)
        if fixture is None:
            rows.append(
                {
                    "week": week, "status": "bye", "opponent": "BYE", "home": None,
                    "kickoff": None, "projected": None, "points": None, "exact": True,
                }
            )
            continue
        opponent = team_abbr(fixture.opponent_id)
        row: dict[str, Any] = {
            "week": week,
            "opponent": f"vs {opponent}" if fixture.home else f"@ {opponent}",
            "home": fixture.home,
            "kickoff": fixture.kickoff,
            "projected": (projections.get(week) or {}).get(identity.player_id),
            "points": None,
            "exact": True,
        }
        if week < current_week:
            row["status"] = "final"
            if actuals is not None:
                line = actuals.get(week)
                if line is None:
                    row["points"] = 0.0
                else:
                    row["points"], row["exact"] = week_points(
                        line, identity.defense, modifiers, unresolved
                    )
        elif week == current_week:
            row["status"] = "live"
            row["points"] = live_points
        else:
            row["status"] = "upcoming"
        rows.append(row)
    return rows


def season_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Points so far, the weeks they came from, and what the rest of the season projects."""
    played = [r for r in rows if r["status"] in ("final", "live") and r["points"] is not None]
    points = round(sum(r["points"] for r in played), 2)
    finals = [r for r in played if r["status"] == "final"]
    return {
        "points": points,
        "weeks": len(finals),
        "average": round(sum(r["points"] for r in finals) / len(finals), 2) if finals else None,
        "projected_remaining": round(
            sum(r["projected"] or 0.0 for r in rows if r["status"] == "upcoming"), 2
        ),
        "exact": all(r["exact"] for r in rows),
    }
