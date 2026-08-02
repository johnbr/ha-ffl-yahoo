"""Turn a fetched league into what entities and cards actually consume.

Pure functions, so the shapes the frontend depends on are pinned by tests
rather than only discovered when a card renders wrong. The coordinator is a
thin shell over this module for exactly that reason: the HA machinery cannot be
unit-tested in this repo's harness, so as little logic as possible lives there.
"""

from __future__ import annotations

from typing import Any

from .const import (
    SCAN_INTERVAL_IDLE_SECONDS,
    SCAN_INTERVAL_LIVE_SECONDS,
    SCAN_INTERVAL_NEAR_GAME_SECONDS,
)
from .plays import PlayFeed, ScoringEvent, describe
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


def _team_dict(team: Any) -> dict[str, Any]:
    return {
        "team_id": team.team_id,
        "name": team.name,
        "points": team.points,
        "projected": team.projected,
    }


def matchup_rows(data: LeagueData) -> list[dict[str, Any]]:
    """Compact per-matchup summaries — the score-only view the cards show at rest.

    Sourced from the **scoreboard** rather than the matchup pages, so a matchup
    whose roster fetch failed still shows its score.
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
        rows.append(
            {
                "matchup_id": matchup_id,
                "index": index + 1,
                "home": _team_dict(home),
                "away": _team_dict(away),
                "leader": leader,
                "has_roster": index < len(data.matchups),
            }
        )
    return rows


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
        "stat_line": player.stat_line,
        "game": player.game_note,
        "game_state": player.game_state,
    }


def play_dict(event: ScoringEvent) -> dict[str, Any]:
    """One scoring event, as the banner and history overlay want it."""
    return {
        "event_id": event.event_id,
        "text": describe(event),
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
) -> dict[str, Any]:
    """Attribute payload for the league scoreboard entity."""
    if data is None:
        return {
            "league_id": league_id,
            "week": None,
            "matchups": [],
            "last_play": None,
            "recent_plays": [],
            "partial": False,
            "source": "web",
        }

    last = feed.last_play()
    return {
        "league_id": league_id,
        "week": data.week,
        "matchups": matchup_rows(data),
        "last_play": play_dict(last) if last else None,
        "recent_plays": [play_dict(e) for e in feed.recent(ATTR_RECENT_PLAYS)],
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
