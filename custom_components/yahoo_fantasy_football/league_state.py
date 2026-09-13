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
from .plays import PlayFeed, ScoringEvent, describe, short_describe
from .web_client import LeagueData

# How many plays ride along in entity attributes. The full history is served on
# demand over WebSocket instead — attributes are pushed to every connected
# client on every state change, so a long list there is pure websocket churn.
ATTR_RECENT_PLAYS = 10


def poll_interval(data: LeagueData | None) -> int:
    """Seconds until the next refresh, from what the games are actually doing.

    Driven by observed game state rather than a season calendar, so a Thursday
    or Monday night game gets live cadence without special-casing, and a
    completed slate drops to idle even if it is still nominally Sunday.

    Unknown game state counts as "something might be happening" — the live
    wording of Yahoo's game note is unverified (see :mod:`yahoo_web`), and
    polling too often is a smaller error than a card that stops updating
    mid-game.
    """
    if data is None:
        return SCAN_INTERVAL_NEAR_GAME_SECONDS

    states = {p.game_state for m in data.matchups for p in m.players}
    if "in" in states:
        return SCAN_INTERVAL_LIVE_SECONDS
    if "pre" in states or "unknown" in states:
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
    }


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
    }


def play_dict(event: ScoringEvent) -> dict[str, Any]:
    """One scoring event, as the banner and history overlay want it."""
    return {
        "event_id": event.event_id,
        "text": describe(event),
        # The compact player-side form. Carried alongside rather than replacing
        # ``text`` so the row can stay terse while the expanded play list keeps
        # Yahoo's full sentence.
        "short_text": short_describe(event),
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
