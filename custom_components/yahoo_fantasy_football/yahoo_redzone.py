"""Parse Yahoo's **GameChannel** data tier into the engine's shapes.

This is the third source in the repo and the one that supersedes the others.
It is what ``sports.yahoo.com/nfl/gamechannel/`` itself runs on, reached with
no OAuth, no cookies, and no account — verified against a league whose privacy
setting is *private*, which :mod:`yahoo_web` cannot read at all.

Two endpoints, both anonymous
-----------------------------
* ``pub-api.fantasysports.yahoo.com/fantasy/v3/redzone/nfl?league_id=…`` — the
  league seed: teams, rosters, matchups, week window, per-player projections,
  and the league's **own scoring modifiers**.
* ``relay-stream.sports.yahoo.com/nfl/{games,stats,players,plays-N}.txt`` — the
  live tier: pipe-delimited, one line per record, refreshed every few seconds.

Why the points are computed here rather than read
-------------------------------------------------
**Yahoo does not publish live fantasy totals on this tier.** ``pfWeek`` stays
``null`` and ``pf`` stays ``0`` for every team while games are in progress —
the browser computes the numbers client-side and so must we: multiply each
player's live stat line by this league's modifier for that stat.

That is not a downgrade. It is *exact* to the league's settings and it removes
the whole class of guesswork in :mod:`yahoo_web`, which had to infer game state
from English strings. Verified live against Yahoo's own StatTracker display:
computed 5.60 / 10.30 / 3.90 for three teams, which is what Yahoo showed.

Scope
-----
Everything here is pure — text and JSON in, dataclasses out. No network, no
clock, no Home Assistant. Fetching belongs to :mod:`redzone_client`.

The dataclasses are deliberately the ones :mod:`yahoo_web` already defines.
Producing the same shapes from a different source means :mod:`league_state`,
:mod:`plays`, :mod:`sensor`, :mod:`websocket` and both Lovelace cards are
untouched by the swap.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .web_client import LeagueData
from .yahoo_web import WebMatchup, WebPlayer, WebTeam

_LOGGER = logging.getLogger(__name__)

REDZONE_HOST = "https://pub-api.fantasysports.yahoo.com"
RELAY_HOST = "https://relay-stream.sports.yahoo.com"

# Yahoo's own value for the desktop GameChannel. Only affects image sizes.
PLAYER_IMAGE_TYPE = "17"


def redzone_url(league_id: str | int, sport: str = "nfl") -> str:
    """The league seed — teams, rosters, matchups and scoring settings."""
    return (
        f"{REDZONE_HOST}/fantasy/v3/redzone/{sport}"
        f"?league_id={league_id}&format=json&player_image_type={PLAYER_IMAGE_TYPE}"
    )


def relay_url(feed: str, sport: str = "nfl") -> str:
    """One live feed: ``games``, ``stats``, ``players`` or ``plays-<playsId>``."""
    return f"{RELAY_HOST}/{sport}/{feed}.txt"


# ---------------------------------------------------------------------------
# Yahoo's stat vocabulary
# ---------------------------------------------------------------------------

# Fantasy stat id -> the name the *relay* feed uses for the same quantity.
# Lifted verbatim from the GameChannel bundle so the two sides line up by
# construction; a league's scoring settings reference these ids.
NFL_STAT_NAMES: dict[int, str] = {
    0: "gamesPlayed", 1: "passingAttempts", 2: "completions", 3: "incompletePasses",
    4: "passingYards", 5: "passingTDs", 6: "passingInterceptions", 7: "sacked",
    8: "rushingAttempts", 9: "rushingYards", 10: "rushingTouchdowns", 11: "receptions",
    12: "receptionYards", 13: "receptionTDs", 14: "returnYards", 15: "returnTDs",
    16: "twoPointConversions", 17: "fumbles", 18: "fumblesLost", 19: "fieldGoalsMade0through19",
    20: "fieldGoalsMade20through29", 21: "fieldGoalsMade30through39",
    22: "fieldGoalsMade40through49", 23: "fieldGoalsMade50plus",
    24: "fieldGoalsMissed0through19", 25: "fieldGoalsMissed20through29",
    26: "fieldGoalsMissed30through39", 27: "fieldGoalsMissed40through49",
    28: "fieldGoalsMissed50plus", 29: "patMade", 30: "patMissed", 31: "pointsAllowed",
    32: "sacks", 33: "interceptions", 34: "fumblesRecovered", 35: "defensiveTDs",
    36: "safeties", 37: "blockedKicks", 38: "tacklesSolo", 39: "tacklesAssisted",
    40: "sacksIDP", 41: "interceptionsIDP", 42: "fumblesForced", 43: "fumblesRecoveredIDP",
    44: "defensiveTDsIDP", 45: "safetiesIDP", 46: "passesDefended", 47: "blockedKicksIDP",
    48: "specialTeamsReturnYards", 49: "specialTeamsReturnTDs", 50: "pointsAllowed0",
    51: "pointsAllowed1through6", 52: "pointsAllowed7through13", 53: "pointsAllowed14through20",
    54: "pointsAllowed21through27", 55: "pointsAllowed28through34", 56: "pointsAllowed35plus",
    57: "offensiveFumbleReturnTDs", 58: "passingPickSixes", 59: "fortyPlusPass",
    60: "fortyPlusPassTD", 61: "fortyPlusRush", 62: "fortyPlusRushTD", 63: "fortyPlusRec",
    64: "fortyPlusRecTD", 65: "tacklesForLossIDP", 66: "turnoverReturnYards",
    67: "fourthDownStops", 68: "tacklesForLoss", 69: "yardsAllowed", 70: "yardsAllowedNegative",
    71: "yardsAllowed0through99", 72: "yardsAllowed100through199",
    73: "yardsAllowed200through299", 74: "yardsAllowed300through399",
    75: "yardsAllowed400through499", 76: "yardsAllowed500plus", 77: "threeAndOuts",
    78: "receivingTargets", 79: "passingFirstDowns", 80: "receivingFirstDowns",
    81: "rushingFirstDowns", 82: "extraPointReturned", 83: "extraPointReturnedIDP",
    84: "fieldGoalsMadeTotalYards", 87: "totalTDs", 88: "totalYards300", 89: "totalYardsBonus",
    90: "totalTurnovers", 91: "totalFieldGoals", 92: "teamWins", 93: "teamLoses",
    94: "totalFumbles", 95: "totalFumblesLost", 96: "totalFumbleRecTds", 97: "kickReturnYards",
    98: "puntReturnYards", 99: "kickRetTds", 100: "puntRetTds", 101: "intRetTds",
    102: "fumbleRecTds", 103: "blkRetTds", 104: "fieldGoalsMade50through59",
    105: "fieldGoalsMade60plus", 106: "fieldGoalsMissed50through59",
    107: "fieldGoalsMissed60plus",
}

# Relay row prefix -> {stat name: column index}. Also lifted from the bundle.
# A player emits one row per stat group they have touched, so a QB who ran
# appears on both ``q`` and ``r``; the groups accumulate into one stat line.
PLAYER_ROWS: dict[str, dict[str, int]] = {
    "o": {"fumbles": 2, "fumblesLost": 3, "offensiveFumbleReturnTDs": 4},
    "q": {
        "passingAttempts": 2, "completions": 3, "incompletePasses": 4, "passingYards": 5,
        "passingTDs": 6, "passingInterceptions": 7, "sacked": 8, "yardsLostToSacks": 9,
        "passingPickSixes": 10, "fortyPlusPass": 11, "fortyPlusPassTD": 12,
        "passingFirstDowns": 13,
    },
    "r": {
        "rushingAttempts": 2, "rushingYards": 3, "rushingTouchdowns": 4, "longestRush": 5,
        "fortyPlusRush": 6, "fortyPlusRushTD": 7, "rushingFirstDowns": 8,
    },
    "w": {
        "receptions": 2, "receptionYards": 3, "receptionTDs": 4, "longestReception": 5,
        "fortyPlusRec": 6, "fortyPlusRecTD": 7, "receivingTargets": 8,
        "receivingFirstDowns": 9,
    },
    "k": {
        "fieldGoalsMade0through19": 2, "fieldGoalsMade20through29": 3,
        "fieldGoalsMade30through39": 4, "fieldGoalsMade40through49": 5,
        "fieldGoalsMade50plus": 6, "fieldGoalsMissed0through19": 7,
        "fieldGoalsMissed20through29": 8, "fieldGoalsMissed30through39": 9,
        "fieldGoalsMissed40through49": 10, "fieldGoalsMissed50plus": 11, "patMade": 12,
        "patMissed": 13, "fieldGoalsMade": 14, "fieldGoalsMissed": 15, "longestFG": 16,
        "fieldGoalsMadeTotalYards": 17, "fieldGoalsMade50through59": 18,
        "fieldGoalsMade60plus": 19, "fieldGoalsMissed50through59": 20,
        "fieldGoalsMissed60plus": 21,
    },
    "n": {"punts": 2, "puntYards": 3, "puntsInside20": 4, "touchbacks": 5},
    "x": {
        "returns": 2, "returnYards": 3, "returnTDs": 4, "twoPointConversions": 5,
        "longestReturn": 6,
    },
    "z": {
        "tacklesSolo": 2, "tacklesAssisted": 3, "sacksIDP": 4, "interceptionsIDP": 5,
        "fumblesForced": 6, "fumblesRecoveredIDP": 7, "defensiveTDsIDP": 8,
        "safetiesIDP": 9, "passesDefended": 10, "blockedKicksIDP": 11,
        "turnoverReturnYards": 12, "tacklesForLossIDP": 13, "extraPointReturnedIDP": 14,
        "blkFgRetTds": 15, "blkPuntRetTds": 16,
    },
}

# Team defence (``f``) is keyed by NFL team id rather than player id, and its
# scoring stats are mostly *derived* from thresholds — see :func:`_defense_stats`.
DEFENSE_ROW: dict[str, int] = {
    "pointsAllowed": 2, "sacks": 3, "interceptions": 4, "fumblesRecovered": 5,
    "defensiveTDs": 6, "safeties": 7, "blockedKicks": 8, "specialTeamsReturnYards": 9,
    "specialTeamsReturnTDs": 10, "sackYards": 11, "tacklesForLoss": 12,
    "yardsAllowed": 13, "fourthDownStops": 14, "threeAndOuts": 15,
    "extraPointReturned": 16, "gamesPlayed": 17, "kickRetTds": 18, "puntRetTds": 19,
    "fumbleRecTds": 20, "intRetTds": 21, "blkFgRetTds": 22, "blkPuntRetTds": 23,
    "kickReturnYards": 24, "puntReturnYards": 25,
}

# ``g`` — one row per NFL game.
GAME_ROW: dict[str, int] = {
    "gameId": 1, "awayTeamId": 2, "homeTeamId": 3, "status": 4, "period": 6,
    "clock": 7, "awayScore": 8, "homeScore": 9, "startTime": 10, "down": 11,
    "distance": 12, "yardsToGoal": 13, "teamWithBall": 14,
}

# ``yardsToGoal`` is yards to the OPPONENT'S goal line, not an absolute spot on
# the field. Established from the per-game plays feed, which uses the same
# column: a 13-yard gain took it 76 -> 63, the next 13-yard gain 63 -> 50, and a
# 5-yard penalty put it back to 55. It counts down as the offence advances, so
# "inside the 20" is literally ``<= 20``.
RED_ZONE_YARDS = 20
# ``playsId`` is the home team id for NFL, i.e. the same column as homeTeamId.
GAME_PLAYS_ID = 3

# ``h`` — one row per club per game: its line score, one cell per period
# played so far, ``h|<playsId>|<teamId>|q1|q2|...``. A club in the third
# quarter has three cells; a finished game has four, or five with overtime.
#
# Worth parsing for one reason: the relay keeps these rows through its morning
# restart, when it has already forgotten the game's status and totals — see
# :func:`settle_forgotten_games`.
LINE_ROW: dict[str, int] = {"playsId": 1, "teamId": 2}
LINE_SCORES_FROM = 3

# Yahoo's NFL club ids, as they appear in the relay's game rows and on every
# player's ``teamId``. The relay prints ids only, so without this table a game
# blurb reads "@ 29" instead of "@ Car". Ids 31 and 32 were never issued —
# Baltimore and Houston took 33 and 34 when they joined.
NFL_TEAMS: dict[str, str] = {
    "1": "Atl", "2": "Buf", "3": "Chi", "4": "Cin", "5": "Cle", "6": "Dal",
    "7": "Den", "8": "Det", "9": "GB", "10": "Ten", "11": "Ind", "12": "KC",
    "13": "LV", "14": "LAR", "15": "Mia", "16": "Min", "17": "NE", "18": "NO",
    "19": "NYG", "20": "NYJ", "21": "Phi", "22": "Ari", "23": "Pit", "24": "LAC",
    "25": "SF", "26": "Sea", "27": "TB", "28": "Was", "29": "Car", "30": "Jax",
    "33": "Bal", "34": "Hou",
}


def team_abbr(team_id: str) -> str:
    """``"29"`` -> ``"Car"``. An unknown id falls back to itself.

    Falling back rather than blanking keeps a relocation or an expansion club
    visible — a bare number is poor, but a missing opponent is worse.
    """
    return NFL_TEAMS.get(str(team_id), str(team_id))


# Relay game status letter -> the vocabulary ``WebPlayer.game_state`` already uses.
GAME_STATUS: dict[str, str] = {
    "S": "pre",      # scheduled
    "P": "in",       # in progress
    "F": "post",     # final
    "FO": "post",    # final, overtime
    "D": "post",     # delayed/postponed - nothing more will score
}


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Relay feeds
# ---------------------------------------------------------------------------


def _relay_lines(text: str) -> list[list[str]]:
    """Split a relay feed into fields, dropping its ``#`` comment header."""
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(line.split("|"))
    return rows


def relay_sequence(text: str) -> int | None:
    """The feed's ``source_sequence_number``.

    **Do not use this as a change detector.** Measured against the live feed
    on 2026-09-09: two fetches 29 s apart carried *different stat lines* (a
    field goal had landed) under the *same* sequence number, while eight
    fetches in a row during a lull were byte-identical. The counter belongs to
    Yahoo's upstream event stream, not to the regeneration of this snapshot, so
    "same sequence" does not mean "same data".

    It is exposed because it is the right correlator for the CometD delta
    channel this feed seeds — Yahoo's own client uses it to spot a gap in the
    delta stream and re-seed. Polling has no such use for it.
    """
    for line in (text or "").splitlines():
        if line.startswith("# source_sequence_number="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                return None
    return None


def parse_relay_stats(text: str) -> dict[str, dict[str, float]]:
    """Live stat lines for every player in today's games, by Yahoo player id.

    The ids are the same ones the fantasy tier uses, so no name matching is
    needed anywhere in this integration.
    """
    players: dict[str, dict[str, float]] = {}
    for cells in _relay_lines(text):
        fields = PLAYER_ROWS.get(cells[0])
        if not fields or len(cells) < 2:
            continue
        acc = players.setdefault(cells[1], {})
        for name, index in fields.items():
            if index < len(cells):
                acc[name] = _num(cells[index])
    return players


def parse_relay_defense(text: str) -> dict[str, dict[str, float]]:
    """Team-defence stat lines, by NFL team id."""
    teams: dict[str, dict[str, float]] = {}
    for cells in _relay_lines(text):
        if cells[0] != "f" or len(cells) < 2:
            continue
        raw = {
            name: _num(cells[index])
            for name, index in DEFENSE_ROW.items()
            if index < len(cells)
        }
        teams[cells[1]] = _defense_stats(raw)
    return teams


def _defense_stats(raw: dict[str, float]) -> dict[str, float]:
    """Add the bucketed stats a D/ST is actually scored on.

    Yahoo scores defences on bands ("0 points allowed", "100-199 yards
    allowed"), not on the raw totals, and the bundle derives those in the
    client. The band flags only mean anything once the game has been played, so
    ``gamesPlayed`` gates the two zero-buckets — otherwise every defence would
    score the shutout bonus before kickoff.
    """
    stats = dict(raw)
    points = raw.get("pointsAllowed", 0.0)
    yards = raw.get("yardsAllowed", 0.0)
    played = raw.get("gamesPlayed", 0.0) == 1

    stats["pointsAllowed0"] = 1.0 if (points == 0 and played) else 0.0
    for name, low, high in (
        ("pointsAllowed1through6", 1, 6),
        ("pointsAllowed7through13", 7, 13),
        ("pointsAllowed14through20", 14, 20),
        ("pointsAllowed21through27", 21, 27),
        ("pointsAllowed28through34", 28, 34),
    ):
        stats[name] = 1.0 if low <= points <= high else 0.0
    stats["pointsAllowed35plus"] = 1.0 if points >= 35 else 0.0

    stats["yardsAllowedNegative"] = 1.0 if yards < 0 else 0.0
    stats["yardsAllowed0through99"] = 1.0 if (0 <= yards <= 99 and played) else 0.0
    for name, low, high in (
        ("yardsAllowed100through199", 100, 199),
        ("yardsAllowed200through299", 200, 299),
        ("yardsAllowed300through399", 300, 399),
        ("yardsAllowed400through499", 400, 499),
    ):
        stats[name] = 1.0 if low <= yards <= high else 0.0
    stats["yardsAllowed500plus"] = 1.0 if yards >= 500 else 0.0

    stats["blkRetTds"] = raw.get("blkFgRetTds", 0.0) + raw.get("blkPuntRetTds", 0.0)
    return stats


def clock_text(period: str, clock: str) -> str:
    """Where a running game stands: ``Q3 6:24``, ``Halftime``, ``OT 8:11``.

    One rule for both cards. The games card's clock and the blurb under a
    player in the expanded lineup are the same fact about the same game, and
    they used to be formatted twice — the lineup printed ``Q2 0:00`` under a
    player whose game the card beside it called ``Halftime``.

    The feed has no separate state for half time — it parks the second
    quarter on 0:00 — so this is the only place it can be named, and naming it
    is both shorter and what a reader is actually asking. Any period past the
    fourth is overtime, which the feed numbers ``5``.
    """
    period, clock = str(period or ""), str(clock or "").strip()
    if period == "2" and clock in {"0:00", "00:00"}:
        return "Halftime"
    if not period:
        return clock
    quarter = f"Q{period}" if period.isdigit() and int(period) <= 4 else "OT"
    return f"{quarter} {clock}".strip()


class GameState:
    """One NFL game as the relay reports it, in the terms the cards want."""

    __slots__ = (
        "away",
        "away_score",
        "clock",
        "distance",
        "down",
        "game_id",
        "home",
        "home_score",
        "period",
        "plays_id",
        "quarters",
        "start_time",
        "status",
        "team_with_ball",
        "yards_to_goal",
    )

    def __init__(self, cells: list[str]) -> None:
        get = lambda i: cells[i] if i < len(cells) else ""  # noqa: E731
        # {club id: points per period played}, from the ``h`` rows that follow
        # the game's own — see :func:`parse_relay_games`.
        self.quarters: dict[str, tuple[int, ...]] = {}
        self.game_id = get(GAME_ROW["gameId"])
        self.away = get(GAME_ROW["awayTeamId"])
        self.home = get(GAME_ROW["homeTeamId"])
        self.status = get(GAME_ROW["status"])
        self.period = get(GAME_ROW["period"])
        self.clock = get(GAME_ROW["clock"])
        self.away_score = get(GAME_ROW["awayScore"])
        self.home_score = get(GAME_ROW["homeScore"])
        self.start_time = get(GAME_ROW["startTime"])
        self.down = get(GAME_ROW["down"])
        self.distance = get(GAME_ROW["distance"])
        self.yards_to_goal = get(GAME_ROW["yardsToGoal"])
        self.team_with_ball = get(GAME_ROW["teamWithBall"])
        self.plays_id = get(GAME_PLAYS_ID)

    @property
    def state(self) -> str:
        return GAME_STATUS.get(self.status, "unknown")

    def has_ball(self, team_id: str) -> bool:
        """Does this club have possession right now?

        Only while the game is actually running: the feed leaves the last
        drive's possession sitting in the row after the whistle, and a football
        beside a player whose game ended an hour ago is just wrong.
        """
        return self.state == "in" and bool(team_id) and team_id == self.team_with_ball

    def in_red_zone(self, team_id: str) -> bool:
        """Possession inside the opponent's 20."""
        if not self.has_ball(team_id):
            return False
        try:
            yards = int(self.yards_to_goal)
        except ValueError:
            return False
        return 0 < yards <= RED_ZONE_YARDS

    def note_for(self, team_id: str) -> str:
        """The game blurb printed beside a player, from that team's side.

        Mirrors what Yahoo shows: an opponent and a kickoff time before the
        game, a score and a clock during it, a result after.
        """
        home = team_id == self.home
        opponent = team_abbr(self.away if home else self.home)
        versus = f"vs {opponent}" if home else f"@ {opponent}"
        mine, theirs = (self.home_score, self.away_score) if home else (
            self.away_score, self.home_score)

        if self.state == "pre":
            return versus
        if self.state == "in":
            return f"{clock_text(self.period, self.clock)} {mine}-{theirs} {versus}"
        if self.state == "post":
            if not (mine and theirs):
                # Over, score unknown — a game the relay forgot without leaving
                # its line score behind (see settle_forgotten_games). "Final"
                # alone beats "Final 0-0", which is a claim.
                return f"Final {versus}"
            try:
                result = "W" if int(mine) > int(theirs) else "L" if int(mine) < int(theirs) else "T"
            except ValueError:
                result = ""
            return f"Final {result} {mine}-{theirs} {versus}".replace("  ", " ")
        return versus


# Minutes in a regulation NFL game. Overtime is deliberately not modelled: a
# game in OT has no scheduled time left, so "nothing more is projected" is both
# the simplest answer and very nearly the right one.
GAME_MINUTES = 60.0


def remaining_fraction(game: GameState | None) -> float:
    """How much of ``game`` is still to be played, as 0.0-1.0.

    ``None`` — a club with no row in the feed — reads as a full game ahead
    rather than none, matching how an unknown game state is treated elsewhere:
    assuming a player is done is the costlier mistake.
    """
    if game is None:
        return 1.0
    state = game.state
    if state == "pre":
        return 1.0
    if state != "in":
        return 0.0
    try:
        period = int(game.period or 1)
        minutes, _, seconds = (game.clock or "0:00").partition(":")
        left_in_period = int(minutes) + int(seconds or 0) / 60.0
    except ValueError:
        return 1.0
    # Quarters already finished, plus whatever is left on the current clock.
    quarters_left = max(0, 4 - period)
    remaining = quarters_left * 15.0 + left_in_period
    return max(0.0, min(1.0, remaining / GAME_MINUTES))


def live_projection(
    points: float | None, projected: float | None, game: GameState | None
) -> float | None:
    """What this player is on pace to finish with.

    Reverse-engineered from Yahoo's own StatTracker and confirmed to the cent
    against four players at a known game clock::

        live = points_so_far + original_projection * fraction_of_game_remaining

    So a player yet to kick off is worth their full projection, one whose game
    is final is worth exactly what they scored, and one mid-game is worth what
    they have plus a pro-rated share of what they were projected for.

    Known deviation: Yahoo models **team defences** differently (its DEF number
    came out well below this formula's while every skill player and the kicker
    matched exactly). A D/ST therefore tracks Yahoo's live number only loosely.
    """
    if projected is None:
        return None if points is None else round(points, 2)
    return round((points or 0.0) + projected * remaining_fraction(game), 2)


def live_signature(games: dict[str, GameState]) -> str:
    """A scalar that changes whenever a live game visibly moves.

    The cards guard their repaint on a SCALAR fingerprint of the entity's
    attributes, and nothing in those attributes used to reflect the game clock:
    a quarter ticking by with nobody scoring produced byte-identical
    attributes, so Home Assistant fired no state change and the open roster
    kept showing the clock it was opened with. This is what makes that visible
    without putting every game's state into the attributes.

    Only games in progress contribute — a finished game's frozen clock must not
    keep the signature churning, and a scheduled one has nothing to say yet.
    """
    parts = sorted(
        f"{g.game_id}:{g.period}:{g.clock}:{g.away_score}-{g.home_score}"
        for g in {id(g): g for g in games.values()}.values()
        if g.state == "in"
    )
    return "|".join(parts)


# ``p|`` — one row per play in one game.
PLAY_ROW: dict[str, int] = {
    "gameKey": 1, "sequence": 2, "down": 3, "distance": 4, "yardsToGoal": 5,
    "teamWithBall": 6, "period": 7, "clock": 8, "playType": 9, "yards": 10,
}
PLAY_TEXT_FROM = 11
"""Everything from here on is description, and there can be more than one part.

A punt is two sentences in two columns — the punt and the return — so the text
is the REST of the row joined back together, not a single cell.
"""

# ``m|`` — the player dictionary for everyone in today's games.
PLAYER_ROW: dict[str, int] = {"id": 1, "teamId": 2, "position": 3, "first": 4, "last": 5}

# Plays name people by id: ``[42654] rushed to the right for 13 yard gain``.
_PLAY_REF = re.compile(r"\[(\d+)\]")

# Substituted for an id the dictionary does not know, so the clause it sits in
# can be dropped whole rather than printed with a hole in it.
_UNKNOWN = "\x00"

# A clause that is NOTHING but tackler attribution — ``tackled by [29298]``,
# or two of them. Matched on the raw clause, ids still bracketed, so a tackle
# that says more ("tackled by [id] in the end zone for a safety") does not
# match and is kept: there the tackle is the play.
_PURE_TACKLE = re.compile(r"^tackled by \[\d+\](?: and \[\d+\])?$")


def _cell(cells: list[str], index: int) -> str:
    """One cell of a relay row, or ``""`` — rows are ragged by design."""
    return cells[index] if index < len(cells) else ""


@dataclass(frozen=True)
class RelayPlay:
    """One play as Yahoo's per-game feed reports it."""

    game_key: str
    sequence: int
    period: str
    clock: str
    team_with_ball: str
    player_ids: tuple[str, ...]
    """Everyone named in the description, in the order they appear."""
    text: str
    """Raw, with ids still in brackets — see :func:`humanize_play`."""
    down: str = ""
    distance: str = ""
    yards_to_goal: str = ""
    """The situation the play was run FROM — ``1st & 10`` at the snap.

    Blank (``0``) on anything that is not a snap from scrimmage: kickoffs,
    PATs, timeouts, the two-minute warning, the end of a quarter. That is
    the feed's own convention, and it is exactly the set of rows that should
    not carry a down-and-distance prefix, so nothing here has to know play
    types.
    """


def parse_relay_players(text: str) -> dict[str, str]:
    """``{player_id: "First Last"}`` for everyone who has appeared this week.

    Yahoo's play descriptions name people by id only, and the league seed knows
    just the ~140 players somebody rosters — not the defender who made the
    tackle. This feed is what makes a play description readable.

    It is not a roster. It tracks the stats feed: a player is added the first
    time a stat line is written for them (measured 2026-09-20, Sunday 10:36 PT:
    16-24 names per club in the first quarter, 36 per club for the game played
    on Thursday, three names added in 46 s across eight games). So a player's
    first touch of the day names an id that an older copy of this dictionary
    cannot resolve — see :func:`play_ids_needed` for how a caller finds out.
    """
    names: dict[str, str] = {}
    for cells in _relay_lines(text):
        if cells[0] != "m":
            continue
        pid = _cell(cells, PLAYER_ROW["id"])
        if not pid:
            continue
        names[pid] = " ".join(
            part
            for part in (_cell(cells, PLAYER_ROW["first"]), _cell(cells, PLAYER_ROW["last"]))
            if part
        )
    return names


def parse_relay_plays(text: str) -> list[RelayPlay]:
    """Every play in one game, oldest first."""
    plays: list[RelayPlay] = []
    for cells in _relay_lines(text):
        if cells[0] != "p":
            continue
        body = "|".join(cells[PLAY_TEXT_FROM:]).strip()
        if not body:
            continue
        try:
            sequence = int(_cell(cells, PLAY_ROW["sequence"]) or 0)
        except ValueError:
            continue
        plays.append(
            RelayPlay(
                game_key=_cell(cells, PLAY_ROW["gameKey"]),
                sequence=sequence,
                period=_cell(cells, PLAY_ROW["period"]),
                clock=_cell(cells, PLAY_ROW["clock"]),
                team_with_ball=_cell(cells, PLAY_ROW["teamWithBall"]),
                player_ids=tuple(_PLAY_REF.findall(body)),
                text=body,
                down=_cell(cells, PLAY_ROW["down"]),
                distance=_cell(cells, PLAY_ROW["distance"]),
                yards_to_goal=_cell(cells, PLAY_ROW["yardsToGoal"]),
            )
        )
    plays.sort(key=lambda p: p.sequence)
    return plays


def humanize_play(text: str, names: dict[str, str]) -> str:
    """Turn ``[42654] rushed ..., tackled by [29298]`` into plain English.

    The tackler is left out. Who made the tackle means nothing in fantasy
    terms and it was the longest part of most plays — "rushed to the right
    for 13 yard gain, tackled by Pat Surtain II" says everything it needs to
    before the comma. Only a clause that is purely the tackler goes; one
    that says more keeps its tackle. A sentence that is NOTHING but the
    tackle goes entirely — the feed does emit "tackled by L'Jarius Sneed" as
    a play's whole text, and printed under a matchup it was a line saying
    who tackled the manager's receiver. Empty is the right answer there:
    every caller has a better fallback (the stat line for a scoring event,
    the previous play for a game) than a tackle with no play in front of it.

    A clause naming somebody the dictionary cannot resolve is dropped whole,
    which is why the substitution goes via a sentinel rather than straight to
    the name: printing "tackled by" with nothing after it is worse than not
    mentioning the tackle. Sentences are separated by ``|`` in the feed (a punt
    and its return), so those are split too.
    """
    sentences = []
    for sentence in text.split("|"):
        kept = _clauses(sentence)
        resolved = [_PLAY_REF.sub(lambda m: names.get(m.group(1), _UNKNOWN), c) for c in kept]
        joined = ", ".join(c for c in resolved if _UNKNOWN not in c)
        if joined:
            sentences.append(joined)
    return ". ".join(sentences)


def _clauses(sentence: str) -> list[str]:
    """A sentence's clauses, minus a pure tackle, ids still bracketed."""
    raw = [c.strip() for c in sentence.split(", ") if c.strip()]
    return [c for c in raw if not _PURE_TACKLE.match(c)]


def play_ids_needed(text: str) -> tuple[str, ...]:
    """The ids :func:`humanize_play` must resolve to keep every clause of ``text``.

    Everyone the play names except a pure tackler: that clause is dropped
    before any name is looked up, so a tackler the dictionary has never heard
    of costs nothing — and the dictionary does omit some, since it tracks the
    stats feed and a tackle alone does not always write a stat line (one
    Tennessee defender was in the play feed from the first quarter and never
    in the dictionary, 2026-09-20). Every other id is what makes the play
    readable: an unresolved subject drops the play's sentence entirely, which
    on the games card was a down missing from the list.

    This is the signal ``RedzoneClient.async_players`` refetches on. Reported
    unfiltered by what the dictionary knows, so a caller can ask before it has
    one.
    """
    return tuple(
        dict.fromkeys(
            pid
            for sentence in text.split("|")
            for clause in _clauses(sentence)
            for pid in _PLAY_REF.findall(clause)
        )
    )


# A word that can be part of a tackler's name in already-humanised text:
# a capitalised word, apostrophe and hyphen names (L'Jarius, Amon-Ra),
# initials (P.J.), the suffixes and particles a roster uses, and "and".
_NAME_WORD = re.compile(r"^(?:[A-Z][\w'-]*|(?:[A-Z]\.)+|Jr\.|Sr\.|St\.|II|III|IV|V|and)$")


def strip_tackler(text: str) -> str:
    """:func:`humanize_play`'s tackler rule, applied to text already humanised.

    For play text that was humanised BEFORE the rule existed and persisted
    with the tackler in — a week's history survives a restart through Home
    Assistant's Store. New text never needs this; it is stripped at source,
    where the ids make the clause unambiguous.

    Here it has to be read back out of English. A clause is dropped when it
    is "tackled by" followed by nothing but name words; a lower-case word
    after the name ("in the end zone for a safety") means it says more, and
    it stays. The tackle is the last clause of its sentence, so a name word
    carrying a full stop ("Holland.") is where the next sentence starts —
    "…tackled by Jevon Holland. NY Giants committed 15 yard penalty" keeps
    the penalty. "St." and "Jr." are name words, so a stop is only read as a
    sentence end when it is on a word that is not one of them.
    """
    kept: list[str] = []
    for clause in text.split(", "):
        clause = clause.strip()
        if not clause.startswith("tackled by "):
            if clause:
                kept.append(clause)
            continue
        words = clause[len("tackled by ") :].split(" ")
        rest: list[str] | None = None
        for i, word in enumerate(words):
            if _NAME_WORD.match(word):
                continue
            if word.endswith(".") and _NAME_WORD.match(word[:-1]):
                rest = words[i + 1 :]  # the sentence after the tackle
                break
            kept.append(clause)  # says more than who tackled
            break
        else:
            continue  # nothing but the tackler: gone
        if rest:
            joined = ", ".join(kept)
            kept = [f"{joined}. {' '.join(rest)}" if joined else " ".join(rest)]
    return ", ".join(kept)


def live_clubs(games: dict[str, GameState]) -> frozenset[str]:
    """Abbreviations of every club whose game is in progress.

    Keyed by abbreviation rather than id because that is what a scoring event
    carries: the play feed records a player's club as ``Sea``, never ``26``.
    """
    clubs: set[str] = set()
    for game in games.values():
        if game.state == "in":
            clubs.add(team_abbr(game.away))
            clubs.add(team_abbr(game.home))
    return frozenset(clubs)


def plays_feeds(games: dict[str, GameState]) -> dict[str, str]:
    """``{club abbreviation: plays-feed id}`` for games in progress.

    Keyed by abbreviation because that is what a scoring event carries, and
    limited to live games because a finished game's plays are already attached
    to the events that came out of it.
    """
    feeds: dict[str, str] = {}
    for game in games.values():
        if game.state != "in":
            continue
        for club in (game.away, game.home):
            feeds[team_abbr(club)] = game.plays_id
    return feeds


def active_game_count(games: dict[str, GameState]) -> int:
    """How many NFL games are in progress right now.

    ``games`` is keyed per participating team, so the game ids are de-duplicated
    here rather than counting each game twice.
    """
    return len({g.game_id for g in games.values() if g.state == "in"})


def parse_relay_games(text: str) -> dict[str, GameState]:
    """Today's NFL games, keyed by **each** participating team's id.

    Keyed per team rather than per game because every lookup here starts from a
    player's NFL team, never from a game.
    """
    games: dict[str, GameState] = {}
    rows = _relay_lines(text)
    for cells in rows:
        if cells[0] != "g":
            continue
        game = GameState(cells)
        games[game.away] = game
        games[game.home] = game
    # Line scores come on their own rows after the game's, keyed by club — so
    # a second pass rather than a reliance on the order.
    for cells in rows:
        if cells[0] != "h":
            continue
        club = _cell(cells, LINE_ROW["teamId"])
        game = games.get(club)
        if game is None or game.plays_id != _cell(cells, LINE_ROW["playsId"]):
            continue
        try:
            game.quarters[club] = tuple(int(c) for c in cells[LINE_SCORES_FROM:])
        except ValueError:
            continue
    return games


# How long after kickoff a game the relay still calls "scheduled" is taken to
# have been played anyway.
#
# The relay restarts each morning and comes back with the schedule only
# (observed 2026-09-18, 08:04 PT: sequence number back to 1, the previous
# night's Det at Buf listed ``S``, 0-0, no clock — while its play feed still
# held all 187 plays and its line-score rows still summed to 31-41). Nothing
# in the game row says the game happened, so the kickoff is the evidence, and
# four hours is longer than any game that is still in progress: a long one
# runs three and a quarter, and one that is actually still on says ``P``.
#
# What is at stake is not the games card: every player in the forgotten game
# reads as not yet played, so the FFL card projects their full day ON TOP of
# the points they already scored — 81 scored, 140 projected, 221 "live" — and
# counts them among the starters still to play. Observed on the morning after.
FORGOTTEN_AFTER_SECONDS = 4 * 3600

# A line score with this many periods is a whole game; overtime is a fifth.
FULL_GAME_PERIODS = 4


def settle_forgotten_games(games: dict[str, GameState], now: float) -> list[GameState]:
    """Mark the games the relay has forgotten as final, in place.

    Two signals, either sufficient: a full line score on a game still listed
    as scheduled — which is a game that has been played, whatever the status
    letter says — or a kickoff more than :data:`FORGOTTEN_AFTER_SECONDS` ago.
    The line score also supplies the final score; without one the score is
    left blank rather than at the feed's 0-0, so the card can say "Final"
    without saying "nil-nil".

    Returns the games it settled, for the log.
    """
    settled: list[GameState] = []
    for game in {id(g): g for g in games.values()}.values():
        if game.state != "pre":
            continue
        away = game.quarters.get(game.away, ())
        home = game.quarters.get(game.home, ())
        played = len(away) >= FULL_GAME_PERIODS and len(home) >= FULL_GAME_PERIODS
        start = _epoch(game.start_time)
        stale = start is not None and now - start >= FORGOTTEN_AFTER_SECONDS
        if not (played or stale):
            continue
        game.status = "FO" if max(len(away), len(home)) > FULL_GAME_PERIODS else "F"
        if played:
            game.away_score, game.home_score = str(sum(away)), str(sum(home))
        else:
            game.away_score = game.home_score = ""
        settled.append(game)
    return settled


def games_in_order(games: dict[str, GameState]) -> list[GameState]:
    """The week's slate, one entry per GAME, in kickoff order.

    :func:`parse_relay_games` keys per TEAM because every fantasy lookup starts
    from a player's club, which means each game appears twice. An NFL-games view
    asks the opposite question — "what is happening across the league" — so it
    needs each game once, including the games no rostered player touches.

    Sorted by kickoff then id so the order is stable across polls; a list that
    reshuffles under a reader is worse than one in an arbitrary but fixed order.
    """
    unique: dict[str, GameState] = {}
    for game in games.values():
        unique[game.game_id] = game

    def key(game: GameState) -> tuple[int, str]:
        try:
            start = int(game.start_time)
        except (TypeError, ValueError):
            start = 0
        return (start, str(game.game_id))

    return sorted(unique.values(), key=key)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def scoring_modifiers(league: dict[str, Any]) -> dict[str, float]:
    """The league's own points-per-stat table, keyed by relay stat name.

    Read from the league rather than assumed, which is the entire reason the
    computed totals match Yahoo's to the cent in a half-PPR, 0.04-per-pass-yard
    league that shares no defaults with the next one.
    """
    modifiers: dict[str, float] = {}
    for stat in league.get("stats") or []:
        if not stat.get("isScoring"):
            continue
        try:
            name = NFL_STAT_NAMES.get(int(stat["id"]))
        except (KeyError, TypeError, ValueError):
            continue
        if name is not None:
            modifiers[name] = float(stat.get("modifier") or 0.0)
    return modifiers


def score(stats: dict[str, float], modifiers: dict[str, float]) -> float:
    """Fantasy points for one stat line under one league's settings."""
    total = sum(modifiers.get(name, 0.0) * value for name, value in stats.items())
    return round(total, 2)


# Stat line rendering: the subset Yahoo prints, in the order it prints it.
_STAT_LINE: tuple[tuple[str, str], ...] = (
    ("completions", "Comp"), ("passingYards", "Pass Yds"), ("passingTDs", "Pass TD"),
    ("passingInterceptions", "Int"), ("rushingAttempts", "Rush"),
    ("rushingYards", "Rush Yds"), ("rushingTouchdowns", "Rush TD"),
    ("receptions", "Rec"), ("receptionYards", "Rec Yds"), ("receptionTDs", "Rec TD"),
    ("returnYards", "Ret Yds"), ("returnTDs", "Ret TD"),
    ("twoPointConversions", "2PT"), ("fumblesLost", "Fum Lost"),
    ("fieldGoalsMade", "FG"), ("patMade", "PAT"),
    # Team defence. ``specialTeamsReturnYards`` earns a tenth of a point in
    # most leagues, so a defence's score moves on kick returns between
    # turnovers — leaving it out made those changes render as a bare "+0.90".
    ("sacks", "Sack"), ("interceptions", "Int"), ("fumblesRecovered", "Fum Rec"),
    ("defensiveTDs", "Def TD"), ("safeties", "Saf"), ("blockedKicks", "Blk"),
    ("specialTeamsReturnTDs", "ST Ret TD"), ("specialTeamsReturnYards", "ST Ret Yds"),
    ("pointsAllowed", "Pts Allow"),
)


# The compact, player-side rendering of a stat delta: ``1 Rec, 1 Rec Yds``
# becomes ``1 rec, 1 yd``.
#
# Under a fantasy matchup the subject is the PLAYER, so a yardage label that
# repeats its own category is noise — the count right before it already said
# "rec". The category is only dropped when something earlier in the same line
# actually established it: ``3 Rush Yds`` standing alone keeps "rush", because
# nothing else says what those yards were for.
#
# Each entry is ``(category, standalone form, form once the category is known)``.
# Labels absent from this table pass through untouched — "FG", "PAT", "Int" and
# the defensive stats are already as short as they get.
_SHORT_STAT: dict[str, tuple[str, str, str]] = {
    "Comp": ("pass", "comp", "comp"),
    "Pass Yds": ("pass", "pass yds", "yds"),
    "Pass TD": ("pass", "pass TD", "TD"),
    "Rush": ("rush", "rush", "rush"),
    "Rush Yds": ("rush", "rush yds", "yds"),
    "Rush TD": ("rush", "rush TD", "TD"),
    "Rec": ("rec", "rec", "rec"),
    "Rec Yds": ("rec", "rec yds", "yds"),
    "Rec TD": ("rec", "rec TD", "TD"),
    "Ret Yds": ("ret", "ret yds", "yds"),
    "Ret TD": ("ret", "ret TD", "TD"),
    "ST Ret Yds": ("ret", "ret yds", "yds"),
    "ST Ret TD": ("ret", "ret TD", "TD"),
}


def shorten_stat_delta(text: str) -> str:
    """``1 Rec, 1 Rec Yds`` -> ``1 rec, 1 yd``.

    Operates on the rendered string rather than the stat dicts because that is
    what a stored :class:`ScoringEvent` carries — history restored from disk
    has the line and not the numbers behind it.
    """
    parts = [part.strip() for part in (text or "").split(",") if part.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        value, _, label = part.partition(" ")
        entry = _SHORT_STAT.get(label)
        if entry is None:
            out.append(part)
            continue
        category, standalone, bare = entry
        short = bare if category in seen else standalone
        seen.add(category)
        if short.endswith("yds") and value.lstrip("+-") == "1":
            short = short[:-1]
        out.append(f"{value} {short}")
    return ", ".join(out)


# Stats the roster's line leaves out even when they are non-zero. The count
# of carries says nothing a manager opened the lineup to learn — the yards
# and the touchdowns are what score — and on a running back's line it was
# the first thing printed. Receptions stay: they score on their own in a PPR
# league. Only the ROSTER line omits these; a scoring event's delta still
# names every stat that moved, since there "1 Rush" may be the whole story
# of a play whose text has not landed yet.
_STAT_LINE_OMIT: frozenset[str] = frozenset({"rushingAttempts"})


def stat_line(stats: dict[str, float]) -> str:
    """``3 Rec, 26 Rec Yds`` — Yahoo's own phrasing for a live stat line.

    Zero-valued stats are dropped, so a line grows as a player actually does
    something instead of printing a wall of noughts. So are the stats in
    :data:`_STAT_LINE_OMIT`, whatever their value.
    """
    # A shutout is a defence's headline stat and its value is zero, so the
    # usual "drop the noughts" rule would hide the best line of the night.
    # ``gamesPlayed`` only appears on a team-defence row, so it marks one.
    keep_zero = {"pointsAllowed"} if "gamesPlayed" in stats else set()

    parts = []
    for name, label in _STAT_LINE:
        if name in _STAT_LINE_OMIT:
            continue
        value = stats.get(name, 0.0)
        if not value and name not in keep_zero:
            continue
        parts.append(f"{value:g} {label}")
    return ", ".join(parts)


def describe_delta(before: dict[str, float], after: dict[str, float]) -> str:
    """``1 Comp, 13 Pass Yds`` — what changed between two stat lines.

    This is the line under each matchup on Yahoo's own GameChannel rail, and it
    is what makes a scoring banner readable: "+0.52" says nothing, "6 Rush Yds"
    says what happened. Negative movement is a stat correction and is rendered
    with its sign so it cannot be mistaken for a play.
    """
    delta = {
        name: after.get(name, 0.0) - before.get(name, 0.0)
        for name in set(after) | set(before)
    }
    parts = []
    for name, label in _STAT_LINE:
        value = delta.get(name, 0.0)
        if not value:
            continue
        parts.append(f"{value:+g} {label}" if value < 0 else f"{value:g} {label}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

# A team defence is **not** identifiable by ``primaryPosition`` — Yahoo sends
# ``null`` there for a D/ST, which read as "not a defence" and silently scored
# every defence in the league at zero. ``positionType`` is the reliable marker:
# ``O`` offence, ``K`` kicker, ``DT`` team defence, and the league's own
# ``positionsToTypes`` maps ``DEF -> DT``.
#
# It also matters because a D/ST's stats live in a different feed row keyed by
# NFL team id, not by the synthetic fantasy id (``100000 + nfl_team_id``, e.g.
# ``100026`` for the Seahawks) that the roster carries.
DEFENSE_POSITION_TYPE = "DT"


def _epoch(value: Any) -> int | None:
    """A relay timestamp cell as an int, or None — the feed leaves it 0 when unknown."""
    try:
        epoch = int(value)
    except (TypeError, ValueError):
        return None
    return epoch if epoch > 0 else None


def _is_defense(entry: dict[str, Any], meta: dict[str, Any]) -> bool:
    return DEFENSE_POSITION_TYPE in (
        str(entry.get("positionType") or "").upper(),
        str(meta.get("positionType") or "").upper(),
    )


def _player(
    entry: dict[str, Any],
    meta: dict[str, Any],
    stats: dict[str, float],
    modifiers: dict[str, float],
    games: dict[str, GameState],
    side: int,
) -> WebPlayer:
    nfl_team = str(meta.get("teamId") or "")
    game = games.get(nfl_team)
    projected = entry.get("projectedPoints")
    projected = float(projected) if projected not in (None, "") else None
    points = score(stats, modifiers) if stats else 0.0
    return WebPlayer(
        player_id=str(entry.get("id") or ""),
        name=str(meta.get("name") or entry.get("id") or ""),
        slot=str(entry.get("position") or ""),
        points=points,
        projected=projected,
        live_projected=live_projection(points, projected, game),
        stat_line=stat_line(stats),
        game_note=game.note_for(nfl_team) if game else "",
        kickoff=_epoch(game.start_time) if game else None,
        has_ball=game.has_ball(nfl_team) if game else False,
        red_zone=game.in_red_zone(nfl_team) if game else False,
        status=str(entry.get("status") or "").upper(),
        team_side=side,
        stats=stats,
        nfl_team=str(meta.get("team") or ""),
        # A club with no row in the games feed reads ``unknown``, never
        # ``bye``. The feed carries the whole week's slate, so a real bye and a
        # feed that arrived short look identical from here — and calling it a
        # bye would drop the poll cadence to idle in the middle of a Sunday.
        game_state_hint=game.state if game else "unknown",
    )


def _roster(
    team: dict[str, Any],
    service: dict[str, Any],
    stats_by_player: dict[str, dict[str, float]],
    defense_by_team: dict[str, dict[str, float]],
    modifiers: dict[str, float],
    games: dict[str, GameState],
    side: int,
) -> list[WebPlayer]:
    meta_all = service.get("players") or {}
    players = []
    for entry in team.get("players") or []:
        pid = str(entry.get("id") or "")
        meta = meta_all.get(pid) or {}
        if _is_defense(entry, meta):
            stats = defense_by_team.get(str(meta.get("teamId") or ""), {})
        else:
            stats = stats_by_player.get(pid, {})
        players.append(_player(entry, meta, stats, modifiers, games, side))
    return players


def _team(team: dict[str, Any], roster: list[WebPlayer]) -> WebTeam:
    """A fantasy team's live total: its **starters**, summed.

    Bench points are carried on the players but never in the team total, which
    is what makes this match the number Yahoo prints.
    """
    projected = team.get("projectedPoints")
    starters = [p for p in roster if p.starter]
    # Yahoo's own "Proj Pts" is exactly this sum — verified to the cent against
    # both sides of a live matchup.
    live = round(sum(p.live_projected or 0.0 for p in starters), 2) if starters else None
    return WebTeam(
        team_id=str(team.get("id") or ""),
        name=str(team.get("name") or ""),
        points=round(sum(p.points or 0.0 for p in starters), 2),
        projected=float(projected) if projected not in (None, "") else None,
        live_projected=live,
        # "in" counts: a player on the field can still move the score. A
        # missing game (``unknown`` — a bye, or a feed that arrived short)
        # does not, or a bye-week starter would keep a matchup open all week.
        remaining=sum(1 for p in starters if p.game_state in ("pre", "in")),
        remaining_var=round(
            sum(max(0.0, (p.live_projected or 0.0) - (p.points or 0.0)) ** 2 for p in starters), 4
        ),
    )


def league_from_payloads(
    redzone: str | dict[str, Any],
    stats_text: str,
    games_text: str,
    league_id: str | int,
    now: float,
) -> LeagueData:
    """Fold one poll's three payloads into the shape the entities consume."""
    service = _service(redzone)
    league = _league(service, league_id)

    week = int((league.get("weekInfo") or {}).get("week") or 0)
    modifiers = scoring_modifiers(league)
    stats_by_player = parse_relay_stats(stats_text)
    defense_by_team = parse_relay_defense(stats_text)
    games = parse_relay_games(games_text)
    # Before the rosters are built: a player's state, note and live projection
    # all come from their game's, and a forgotten game says "not yet".
    settled = settle_forgotten_games(games, now)
    if settled:
        _LOGGER.debug(
            "relay lists %d played game(s) as scheduled; settled: %s",
            len(settled),
            ", ".join(f"{g.game_id} {g.away_score or '?'}-{g.home_score or '?'}" for g in settled),
        )
    teams = league.get("teams") or {}

    matchups: list[WebMatchup] = []
    standings: list[tuple[WebTeam, WebTeam]] = []

    for group in league.get("matchupGroups") or []:
        for pair in group.get("matchups") or []:
            if len(pair) != 2:
                _LOGGER.debug("Skipping malformed matchup %r", pair)
                continue
            sides: list[WebTeam] = []
            players: list[WebPlayer] = []
            for side, team_id in enumerate(pair):
                team = teams.get(str(team_id)) or {}
                roster = _roster(
                    team, service, stats_by_player, defense_by_team, modifiers, games, side
                )
                players.extend(roster)
                sides.append(_team(team, roster))
            standings.append((sides[0], sides[1]))
            matchups.append(WebMatchup(week=week, teams=(sides[0], sides[1]), players=players))

    return LeagueData(
        week=week,
        matchups=matchups,
        standings=standings,
        fetched_at=now,
        partial=not matchups,
        active_games=active_game_count(games),
        live_tick=live_signature(games),
        # ``None`` when the games feed itself is missing — which is NOT the same
        # as "no game is live", and must not silently blank every play line.
        live_clubs=live_clubs(games) if games else None,
        plays_feeds=plays_feeds(games),
        nfl_games=games_in_order(games),
    )


def _service(redzone: str | dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(redzone) if isinstance(redzone, str) else redzone
    service = (payload or {}).get("service")
    if not isinstance(service, dict):
        raise ValueError("redzone payload has no 'service' object")
    return service


def _league(service: dict[str, Any], league_id: str | int) -> dict[str, Any]:
    leagues = service.get("leagues") or {}
    league = leagues.get(str(league_id))
    if league is None:
        # Yahoo echoes the id it was asked for, so a miss means the league does
        # not exist or the id was mistyped — worth saying rather than KeyError.
        raise ValueError(
            f"league {league_id} not present in the redzone payload "
            f"(got {sorted(leagues) or 'nothing'})"
        )
    return league


def league_name(redzone: str | dict[str, Any], league_id: str | int) -> str:
    """The league's display name, for the config flow's entry title."""
    return str(_league(_service(redzone), league_id).get("name") or "")


def team_choices(redzone: str | dict[str, Any], league_id: str | int) -> dict[str, str]:
    """``{team_id: name}`` so the config flow can offer a picker.

    Anonymous access reports ``isOwned: false`` for every team — there is no
    session to own one — so which team is "mine" has to be chosen, not detected.
    """
    teams = _league(_service(redzone), league_id).get("teams") or {}
    return {
        str(tid): str(team.get("name") or tid)
        for tid, team in sorted(teams.items(), key=lambda kv: int(kv[0]))
    }


def to_snapshot(
    matchups: list[WebMatchup],
    week: int,
    taken_at: float,
    league_id: str | int = "0",
):
    """Fold parsed matchups into a :class:`plays.LeagueSnapshot`.

    Distinct from :func:`yahoo_web.to_snapshot` only in that it carries the raw
    stat line through, which is what lets the play feed say "6 Rush Yds"
    instead of "+0.60".
    """
    from .plays import LeagueSnapshot, PlayerSnapshot

    players: dict[str, PlayerSnapshot] = {}
    for index, matchup in enumerate(matchups):
        matchup_id = f"w{week}.m{index + 1}"
        for player in matchup.players:
            team = matchup.teams[player.team_side]
            key = f"{league_id}.p.{player.player_id}"
            players[key] = PlayerSnapshot(
                player_key=key,
                name=player.name,
                points=float(player.points or 0.0),
                team_key=f"{league_id}.t.{team.team_id}",
                matchup_id=matchup_id,
                nfl_team=player.nfl_team or None,
                position=player.slot,
                selected_position=player.slot,
                stats=dict(player.stats),
            )
    return LeagueSnapshot(week=week, taken_at=taken_at, players=players)
