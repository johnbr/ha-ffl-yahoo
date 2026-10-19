"""What the automation hooks say: bus event payloads and the matchup event entity.

Pure, like ``plays`` and ``league_state``, so it is tested without Home
Assistant. The coordinator calls these to build what it fires on the bus; the
``event`` platform calls :func:`entity_event_type` to turn a bus event into one
of its three event types.

Two bus events:

* ``yahoo_fantasy_football_scoring_play`` — every scoring play in the league,
  as it always was, now also carrying ``entry_id`` and, for a play in your own
  matchup, ``side`` (``mine`` or ``opponent``).
* ``yahoo_fantasy_football_lead_change`` — your matchup changed hands.
"""

from __future__ import annotations

from typing import Any

from .league_state import play_dict
from .plays import ScoringEvent

SIDE_MINE = "mine"
SIDE_OPPONENT = "opponent"

EVENT_TYPE_MY_PLAYER_SCORED = "my_player_scored"
EVENT_TYPE_OPPONENT_SCORED = "opponent_scored"
EVENT_TYPE_LEAD_CHANGE = "lead_change"
EVENT_TYPES = [EVENT_TYPE_MY_PLAYER_SCORED, EVENT_TYPE_OPPONENT_SCORED, EVENT_TYPE_LEAD_CHANGE]


def play_side(event: ScoringEvent, mine: dict[str, Any] | None) -> str | None:
    """``mine``/``opponent`` for a play in your matchup, else ``None``.

    ``mine`` is :func:`league_state.find_team`'s answer for the configured
    team, or ``None`` when no team is configured or it was not found.
    """
    if mine is None or event.matchup_id != mine["matchup_id"]:
        return None
    if str(event.team_key).endswith(f".t.{mine['me']['team_id']}"):
        return SIDE_MINE
    if str(event.team_key).endswith(f".t.{mine['opponent']['team_id']}"):
        return SIDE_OPPONENT
    return None


def scoring_play_payload(
    event: ScoringEvent, entry_id: str, league_id: str, mine: dict[str, Any] | None
) -> dict[str, Any]:
    """The ``scoring_play`` bus event's data."""
    return {
        **play_dict(event),
        "entry_id": entry_id,
        "league_id": league_id,
        "side": play_side(event, mine),
    }


def entity_event_type(payload: dict[str, Any]) -> str | None:
    """Which of the entity's event types a ``scoring_play`` payload is, if any.

    Only plays that moved a score count: a bench player's points do not, and
    a stat correction or a negative play (a fumble lost) is not someone
    scoring. All of those still reach the bus event.
    """
    if not payload.get("starter") or payload.get("correction"):
        return None
    if (payload.get("delta") or 0) <= 0:
        return None
    side = payload.get("side")
    if side == SIDE_MINE:
        return EVENT_TYPE_MY_PLAYER_SCORED
    if side == SIDE_OPPONENT:
        return EVENT_TYPE_OPPONENT_SCORED
    return None


class LeadTracker:
    """Notices when your matchup changes hands, one refresh to the next.

    A tie is not a change of hands: me → tie → me is no change, me → tie →
    opponent is one, reported when the opponent goes ahead. Going ahead from
    0-0 is taking the lead. But the first leader seen at any other score is a
    baseline, not a change: after a restart there is no telling who led before.
    """

    def __init__(self) -> None:
        self._matchup_id: str | None = None
        self._leader: str | None = None
        """The last team seen ahead, or ``None`` before anyone has been."""
        self._known = False
        """Whether ``_leader`` is the truth — set by a leader or a 0-0 start."""

    def observe(self, mine: dict[str, Any] | None, entry_id: str, league_id: str) -> dict[str, Any] | None:
        """Feed one refresh's ``find_team`` answer; the bus payload on a change."""
        if mine is None:
            return None
        me, opponent = mine["me"], mine["opponent"]
        if mine["matchup_id"] != self._matchup_id:
            self._matchup_id, self._leader, self._known = mine["matchup_id"], None, False
        if me["points"] is None or opponent["points"] is None:
            return None
        if me["points"] == opponent["points"]:
            if me["points"] == 0 and self._leader is None:
                self._known = True
            return None

        leader = me["team_id"] if me["points"] > opponent["points"] else opponent["team_id"]
        previous, known = self._leader, self._known
        self._leader, self._known = leader, True
        if not known or leader == previous:
            return None
        leading, trailing = (me, opponent) if leader == me["team_id"] else (opponent, me)
        return {
            "entry_id": entry_id,
            "league_id": league_id,
            "matchup_id": mine["matchup_id"],
            "side": SIDE_MINE if leader == me["team_id"] else SIDE_OPPONENT,
            "leader": leading["name"],
            "leader_points": leading["points"],
            "trailer": trailing["name"],
            "trailer_points": trailing["points"],
            "my_points": me["points"],
            "opponent_points": opponent["points"],
        }
