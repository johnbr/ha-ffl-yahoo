"""Turn a fetched league into what entities and cards actually consume.

Pure functions, so the shapes the frontend depends on are pinned by tests
rather than only discovered when a card renders wrong. The coordinator is a
thin shell over this module for exactly that reason: the HA machinery cannot be
unit-tested in this repo's harness, so as little logic as possible lives there.
"""

from __future__ import annotations

import math
from typing import Any

from .const import (
    SCAN_INTERVAL_IDLE_SECONDS,
    SCAN_INTERVAL_LIVE_SECONDS,
    SCAN_INTERVAL_NEAR_GAME_SECONDS,
)
from .plays import PlayFeed, ScoringEvent, abbreviate_name, describe, short_describe, short_parts
from .web_client import LeagueData
from .yahoo_redzone import clock_text

# How many plays ride along in entity attributes. The full history is served on
# demand over WebSocket instead — attributes are pushed to every connected
# client on every state change, so a long list there is pure websocket churn.
ATTR_RECENT_PLAYS = 10


def poll_interval(data: LeagueData | None, previous: int | None = None) -> int:
    """Seconds until the next refresh, from what the games are actually doing.

    Driven by observed game state rather than a season calendar, so a Thursday
    or Monday night game gets live cadence without special-casing, and a
    completed slate drops to idle even if it is still nominally Sunday.

    Unknown game state counts as "something might be happening" — the live
    wording of Yahoo's game note is unverified (see :mod:`yahoo_web`), and
    polling too often is a smaller error than a card that stops updating
    mid-game.

    ``previous`` is the interval this poll was reached on. It exists to stop a
    slate that says nothing from slowing the poll that would have made it say
    something again — see below.
    """
    if data is None:
        return SCAN_INTERVAL_NEAR_GAME_SECONDS

    states = {p.game_state for m in data.matchups for p in m.players}
    if "in" in states:
        return SCAN_INTERVAL_LIVE_SECONDS
    # A slate where EVERY player is unknown is not an observation about the
    # games; it is what a missing games feed looks like from in here (see
    # ``game_state_hint``). Taking the cadence from it is how one dropped
    # request bought itself five minutes of blank card on 2026-09-25: the
    # blank slowed the very poll that would have cleared it, from 10 s to 300.
    #
    # So a slate like that may never slow the cadence — only a poll that had a
    # feed to read can. Capped at the near-game interval so it cannot speed one
    # up either, which leaves a genuinely empty slate exactly where it was.
    if previous is not None and states and states <= {"unknown"}:
        return min(previous, SCAN_INTERVAL_NEAR_GAME_SECONDS)
    # A delayed game restarts without notice; polling as if it were about to
    # kick off is what catches the restart.
    if states & {"pre", "unknown", "delayed"}:
        return SCAN_INTERVAL_NEAR_GAME_SECONDS
    return SCAN_INTERVAL_IDLE_SECONDS


# How much a still-to-come projection can miss by, as a fraction of itself.
#
# Fantasy scoring is wildly dispersed — a receiver projected for 12 finishes
# anywhere from 0 to 30 — so a win probability that ignored the spread would
# read 99% off a two-point lead. Calibrated against Yahoo's own published
# number on a live matchup (it showed 41/59 where this gives 40/60), which is
# as close as an independent model gets without their inputs.
PROJECTION_CV = 0.75


def win_probability(home: Any, away: Any) -> float | None:
    """Home team's chance of winning, 0.0-1.0, or ``None`` if unknowable.

    A normal approximation on the difference of the two live projections: the
    lead is what is known, and the spread comes from how much is still to be
    played. Once every game is final there is nothing left to be uncertain
    about, so it collapses to a flat 1 or 0 rather than dividing by zero.
    """
    if home.live_projected is None or away.live_projected is None:
        return None
    lead = home.live_projected - away.live_projected
    sigma = PROJECTION_CV * math.sqrt((home.remaining_var or 0.0) + (away.remaining_var or 0.0))
    if sigma <= 0:
        return 1.0 if lead > 0 else 0.0 if lead < 0 else 0.5
    return round(0.5 * (1.0 + math.erf(lead / (sigma * math.sqrt(2.0)))), 4)


def _team_dict(team: Any) -> dict[str, Any]:
    return {
        "team_id": team.team_id,
        "name": team.name,
        "points": team.points,
        "projected": team.projected,
        "live_projected": team.live_projected,
        "remaining": getattr(team, "remaining", None),
    }


def matchup_final(home: Any, away: Any) -> bool:
    """Is this matchup's result in — nobody on either side left to play?

    Both sides have to KNOW they have nobody left; a tier that cannot count
    (``remaining`` of ``None``) never declares a winner. This is what colours
    the winning score, so a false positive would crown a team mid-game.
    """
    return getattr(home, "remaining", None) == 0 and getattr(away, "remaining", None) == 0


def matchup_rows(data: LeagueData, feed: PlayFeed | None = None) -> list[dict[str, Any]]:
    """Compact per-matchup summaries — the score-only view the cards show at rest.

    Sourced from the **scoreboard** rather than the matchup pages, so a matchup
    whose roster fetch failed still shows its score.

    When a ``feed`` is supplied each row also carries **its own** last scoring
    play, which is what the league card prints under each matchup and what
    Yahoo's GameChannel rail shows. A single league-wide banner cannot answer
    "what just moved *this* game", which is the question someone watching five
    matchups at once is actually asking.
    """
    rows: list[dict[str, Any]] = []
    for index, (home, away) in enumerate(data.standings):
        matchup_id = f"w{data.week}.m{index + 1}"
        leader = None
        if home.points is not None and away.points is not None:
            if home.points > away.points:
                leader = home.team_id
            elif away.points > home.points:
                leader = away.team_id
        last = (
            feed.last_play(
                matchup_id=matchup_id,
                starters_only=True,
                # A play must not outlive the game it happened in: once that
                # club is done the line falls back to the newest play from a
                # game still running, and shows nothing when none is.
                nfl_teams=data.live_clubs,
            )
            if feed is not None
            else None
        )
        rows.append(
            {
                "matchup_id": matchup_id,
                "index": index + 1,
                "home": _team_dict(home),
                "away": _team_dict(away),
                "leader": leader,
                "win_prob": win_probability(home, away),
                "final": matchup_final(home, away),
                "has_roster": index < len(data.matchups),
                "last_play": _row_play(last, home, away),
            }
        )
    return rows


def _row_play(event: ScoringEvent | None, home: Any, away: Any) -> dict[str, Any] | None:
    """One row's last play, tagged with **which side of the row scored it**.

    Without the side the card can only print the delta in a fixed corner, which
    reads as belonging to whichever team happens to sit there. The card aligns
    the whole line to the scoring team instead, so "who just gained these
    points" is answered by position rather than by cross-referencing names.
    """
    if event is None:
        return None
    play = play_dict(event)
    play["side"] = _side_of(event.team_key, home, away)
    return play


def _side_of(team_key: str | None, home: Any, away: Any) -> str | None:
    """``"home"``/``"away"`` for a fantasy team key, or ``None`` if neither.

    Team keys are ``{league_id}.t.{team_id}``; the row carries only team ids, so
    the suffix is what ties the two together.
    """
    if not team_key:
        return None
    for name, team in (("home", home), ("away", away)):
        if str(team_key).endswith(f".t.{team.team_id}"):
            return name
    return None


def player_rows(data: LeagueData, matchup_index: int) -> dict[str, Any]:
    """Both rosters for one matchup — the click-to-expand popup payload.

    Served over WebSocket on demand rather than through entity attributes,
    because full rosters for a whole league are far too large to push on every
    state change.
    """
    if not 0 <= matchup_index < len(data.matchups):
        return {"matchup_id": None, "sides": []}

    matchup = data.matchups[matchup_index]
    sides = []
    for side in (0, 1):
        team = matchup.teams[side]
        roster = matchup.roster(side)
        sides.append(
            {
                **_team_dict(team),
                "starters": [_player_dict(p) for p in roster if p.starter],
                "bench": [_player_dict(p) for p in roster if not p.starter],
            }
        )
    return {"matchup_id": f"w{data.week}.m{matchup_index + 1}", "sides": sides}


def _player_dict(player: Any) -> dict[str, Any]:
    return {
        "player_id": player.player_id,
        "name": player.name,
        # "J. Allen", the form the play line uses — the lineup prints this
        # one. A defence is a single word ("Ravens") and passes through.
        "short_name": abbreviate_name(player.name),
        "slot": player.slot,
        "points": player.points,
        "projected": player.projected,
        "live_projected": getattr(player, "live_projected", None),
        "nfl_team": getattr(player, "nfl_team", "") or "",
        "has_ball": bool(getattr(player, "has_ball", False)),
        "red_zone": bool(getattr(player, "red_zone", False)),
        "status": getattr(player, "status", "") or "",
        "stat_line": player.stat_line,
        "game": player.game_note,
        "game_state": player.game_state,
        "kickoff": getattr(player, "kickoff", None),
    }


def play_dict(event: ScoringEvent) -> dict[str, Any]:
    """One scoring event, as the banner and history overlay want it."""
    short_who, short_what = short_parts(event)
    return {
        "event_id": event.event_id,
        "text": describe(event),
        # The compact player-side form. Carried alongside rather than replacing
        # ``text`` so the row can stay terse while the expanded play list keeps
        # Yahoo's full sentence.
        "short_text": short_describe(event),
        # ...and its two halves, so the row can break between the player and
        # what they did rather than wherever the width happens to run out.
        "short_who": short_who,
        "short_what": short_what,
        "stat_delta": event.stat_delta,
        "player": event.player_name,
        "team_key": event.team_key,
        "matchup_id": event.matchup_id,
        "delta": round(event.delta, 2),
        "points": round(event.new_points, 2),
        "timestamp": event.timestamp,
        "correction": event.correction,
        "starter": event.starter,
        "enriched": event.enriched,
    }


# ---------------------------------------------------------------------------
# The NFL slate
# ---------------------------------------------------------------------------


def _clock_text(game: Any) -> str:
    """What to print where the clock goes: kickoff, quarter, or Final.

    Three states a reader cares about, and they want different things: a game
    that has not started is asking "when", one in progress is asking "how far
    in", and a finished one only needs saying so.
    """
    state = getattr(game, "state", "unknown")
    if state == "post":
        return "Final"
    if state == "pre":
        return ""  # the card formats the kickoff time in the viewer's zone
    if state == "delayed":
        # Where the clock stopped is worth keeping: "with two minutes left"
        # is the first thing anyone asks about a delayed game.
        return f"Delayed · {clock_text(getattr(game, 'period', ''), getattr(game, 'clock', ''))}".rstrip(" ·")
    # Half time and overtime are named by the same rule the lineup's player
    # blurb uses, so the two cards never disagree about the same game.
    return clock_text(getattr(game, "period", ""), getattr(game, "clock", ""))


def _yards_to_goal(game: Any) -> int | None:
    """How far the offence has to go, or None when the feed has no real spot.

    None unless a live game has one: 0 and 100 are not positions, and the feed
    parks at 0 between plays, after a score and during a kickoff. The carrier
    must also be one of the two clubs actually playing. The feed parks THAT at
    "0" when nobody has the ball — at half time, between quarters, on a
    kickoff — and "0" is a truthy string, so a bare emptiness check let it
    through and rendered a yard line owned by club "0". Observed live at half
    time on 2026-09-13 as "0 45".

    One gate for everything derived from the spot — the yard-line text, the
    down and distance, the field bar — so they can never disagree about
    whether there is a ball to talk about.
    """
    if getattr(game, "state", "") != "in":
        return None
    try:
        to_goal = int(getattr(game, "yards_to_goal", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not 0 < to_goal < 100:
        return None
    with_ball = str(getattr(game, "team_with_ball", "") or "")
    if with_ball not in {str(game.away), str(game.home)}:
        return None
    return to_goal


def _ball_spot(game: Any) -> str:
    """``DAL 19`` — which yard line the ball is on, the way a broadcast says it.

    The feed sends yards-to-GOAL, one number counting down as the offence
    advances. A yard line is that same fact said the way people read it: a
    number from 1 to 50 plus whose half of the field it is on. Past midfield
    the number belongs to the DEFENDING club — Yahoo's own rail shows
    "2nd & 7, DAL 19" with the red-zone badge on NYG, because it is the Giants
    who have the ball nineteen yards from Dallas's end zone. So this has to
    know who is carrying it, not merely how far they have to go.
    """
    to_goal = _yards_to_goal(game)
    if to_goal is None:
        return ""
    if to_goal == 50:
        return "50"  # midfield belongs to nobody

    from .yahoo_redzone import team_abbr

    with_ball = str(game.team_with_ball)
    if to_goal > 50:
        # Still in their own half: count up from their own goal line.
        return f"{team_abbr(with_ball)} {100 - to_goal}"
    other = game.home if with_ball == str(game.away) else game.away
    return f"{team_abbr(other)} {to_goal}"


def _situation(game: Any) -> str:
    """``2nd & 7`` — down and distance, empty unless a live game has them.

    Gated on there being a real ball spot, because the feed does NOT clear down
    and distance when a drive ends: observed live on 2026-09-13, a game sitting
    between a PAT and the kickoff still reported ``down=1 dist=3`` from the
    snap before the touchdown, with yards-to-goal already zeroed. Printing that
    would caption a play that has already finished. The two render as one
    phrase anyway, so half of it going stale is worse than neither showing.
    """
    if getattr(game, "state", "") != "in":
        return ""
    if not _ball_spot(game):
        return ""
    return down_and_distance(
        getattr(game, "down", 0), getattr(game, "distance", 0), _yards_to_goal(game)
    )


def down_and_distance(down: Any, distance: Any, to_goal: Any = None) -> str:
    """``2nd & 7``, or ``2nd & goal`` — empty when there is no down.

    "Goal" whenever the distance reaches the goal line, not only when the feed
    says so. The games feed updates its fields piecemeal — the spot moves,
    then the down and distance follow a few seconds later — and in between it
    can say things like "4th & 6" with the ball on the 1 (seen live
    2026-09-17; it was 1st & goal). Nothing can be further to go than the
    end zone, so the distance is capped at what the spot allows.
    """
    try:
        down = int(down or 0)
        distance = int(distance or 0)
        to_goal = int(to_goal or 0)
    except (TypeError, ValueError):
        return ""
    if not down:
        return ""
    ordinal = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}.get(down, f"{down}th")
    if distance <= 0 or (0 < to_goal <= distance):
        return f"{ordinal} & goal"
    return f"{ordinal} & {distance}"


def _nfl_side(game: Any, club: str, score: Any) -> dict[str, Any]:
    from .yahoo_redzone import team_abbr

    return {
        "team_id": str(club),
        "abbr": team_abbr(club),
        "score": _int_or_none(score),
        "has_ball": bool(game.has_ball(club)) if hasattr(game, "has_ball") else False,
        "red_zone": bool(game.in_red_zone(club)) if hasattr(game, "in_red_zone") else False,
    }


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def nfl_game_rows(
    data: LeagueData | None, last_plays: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """The week's NFL games, as the games card renders them.

    Small enough to ride in attributes — sixteen games of scalars, against the
    rosters that are deliberately kept out.

    ``last_plays`` maps a plays-feed id to that game's newest play, and is the
    one thing here the games feed cannot supply. The coordinator fetches it for
    LIVE games only and on its own slower cadence, because each game's feed is
    ~20 KB and a full Sunday on the 10 s poll would be a quarter of a megabyte
    every ten seconds. The full per-game play LIST is still not here: those load
    on demand when a game is expanded, the same trade the rosters already make.
    """
    if data is None:
        return []
    rows: list[dict[str, Any]] = []
    for game in data.nfl_games:
        rows.append(
            {
                "game_id": str(game.game_id),
                "plays_id": str(getattr(game, "plays_id", "") or ""),
                "state": getattr(game, "state", "unknown"),
                "clock_text": _clock_text(game),
                "situation": _situation(game),
                "ball_on": _ball_spot(game),
                # Numeric twin of ball_on, for the field bar. None whenever
                # ball_on is empty, by construction.
                "yards_to_goal": _yards_to_goal(game),
                "last_play": (last_plays or {}).get(str(getattr(game, "plays_id", "") or "")) or "",
                "start_time": _int_or_none(getattr(game, "start_time", None)),
                "away": _nfl_side(game, game.away, getattr(game, "away_score", None)),
                "home": _nfl_side(game, game.home, getattr(game, "home_score", None)),
            }
        )
    return rows


def nfl_games_state(data: LeagueData | None) -> str:
    """Low-churn state: how many games are in progress.

    The slate itself belongs in attributes; the recorder only wants a number
    that changes a handful of times a day.
    """
    if data is None:
        return "unknown"
    return str(data.active_games)


def nfl_games_attributes(
    data: LeagueData | None, league_id: str, last_plays: dict[str, str] | None = None
) -> dict[str, Any]:
    rows = nfl_game_rows(data, last_plays)
    return {
        "league_id": league_id,
        "games": rows,
        "active_games": data.active_games if data else 0,
        "live_tick": data.live_tick if data else "",
        "total_games": len(rows),
    }


def scoreboard_attributes(
    data: LeagueData | None,
    feed: PlayFeed,
    league_id: str,
    league_name: str = "",
) -> dict[str, Any]:
    """Attribute payload for the league scoreboard entity.

    ``league_name`` rides along so the card can title itself without having to
    reverse-engineer it from the entity's friendly name.
    """
    if data is None:
        return {
            "league_id": league_id,
            "league_name": league_name,
            "week": None,
            "matchups": [],
            "last_play": None,
            "recent_plays": [],
            "active_games": 0,
            "live_tick": "",
            "partial": False,
            "source": "web",
        }

    last = feed.last_play(starters_only=True, nfl_teams=data.live_clubs)
    return {
        "league_id": league_id,
        "league_name": league_name,
        "week": data.week,
        "matchups": matchup_rows(data, feed),
        "last_play": play_dict(last) if last else None,
        "recent_plays": [
            play_dict(e)
            for e in feed.recent(
                ATTR_RECENT_PLAYS, starters_only=True, nfl_teams=data.live_clubs
            )
        ],
        "active_games": data.active_games,
        # Scalar, and deliberately in the attributes: it is what tells Home
        # Assistant that something moved when a quarter ticks by with no
        # scoring, and what lets the card's repaint guard through.
        "live_tick": data.live_tick,
        "partial": data.partial,
        "source": "web",
    }


def scoreboard_state(data: LeagueData | None) -> str:
    """A deliberately low-churn state.

    The entity's *state* is written to the recorder on every change, so it must
    not carry a live score. Encoding week and matchup count means it changes
    roughly once a week instead of every 45 seconds.
    """
    if data is None:
        return "unknown"
    return f"w{data.week}-{len(data.standings)}"


def find_team(data: LeagueData | None, team_id: str | None) -> dict[str, Any] | None:
    """Locate one team's row and its matchup — powers the my-team entity."""
    if data is None or not team_id:
        return None
    for row in matchup_rows(data):
        for side in ("home", "away"):
            if row[side]["team_id"] == str(team_id):
                return {
                    "matchup_id": row["matchup_id"],
                    "index": row["index"],
                    "me": row[side],
                    "opponent": row["away" if side == "home" else "home"],
                    "winning": row["leader"] == str(team_id),
                }
    return None
