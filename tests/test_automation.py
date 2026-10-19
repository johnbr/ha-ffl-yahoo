"""The automation hooks: bus payloads, the event entity's types, lead changes."""

from __future__ import annotations

from yahoo_fantasy_football.automation import (
    EVENT_TYPE_MY_PLAYER_SCORED,
    EVENT_TYPE_OPPONENT_SCORED,
    LeadTracker,
    entity_event_type,
    play_side,
    scoring_play_payload,
)
from yahoo_fantasy_football.league_state import find_team
from yahoo_fantasy_football.plays import ScoringEvent
from yahoo_fantasy_football.web_client import LeagueData
from yahoo_fantasy_football.yahoo_web import WebTeam

LEAGUE = "802904"
ENTRY = "entry1"
WEEK = 6
MINE = "3"


def _league(my_points: float | None = 0.0, their_points: float | None = 0.0, week: int = WEEK) -> LeagueData:
    """Two matchups; team 3 (mine) plays team 4 in the second."""
    return LeagueData(
        week=week,
        standings=[
            (WebTeam("1", "Alpha", 10.0), WebTeam("2", "Bravo", 12.0)),
            (WebTeam(MINE, "Tesla", my_points), WebTeam("4", "Kush", their_points)),
        ],
    )


def _mine(my_points: float | None = 0.0, their_points: float | None = 0.0, week: int = WEEK):
    return find_team(_league(my_points, their_points, week), MINE)


def _event(team_id: str, matchup: int, delta: float = 6.0, **kw) -> ScoringEvent:
    return ScoringEvent(
        event_id=kw.pop("event_id", "e1"),
        week=WEEK,
        timestamp=1.0,
        player_key="p.1",
        player_name="D. Maye",
        team_key=f"461.l.{LEAGUE}.t.{team_id}",
        matchup_id=f"w{WEEK}.m{matchup}",
        nfl_team="NE",
        position="QB",
        delta=delta,
        old_points=0.0,
        new_points=delta,
        starter=kw.pop("starter", True),
        correction=kw.pop("correction", False),
        **kw,
    )


# --- which side a play is on -------------------------------------------------


def test_a_play_by_my_team_is_mine() -> None:
    assert play_side(_event(MINE, 2), _mine()) == "mine"


def test_a_play_by_my_opponent_is_theirs() -> None:
    assert play_side(_event("4", 2), _mine()) == "opponent"


def test_a_play_in_another_matchup_has_no_side() -> None:
    assert play_side(_event("1", 1), _mine()) is None


def test_no_configured_team_means_no_side() -> None:
    assert play_side(_event(MINE, 2), None) is None


def test_team_ids_match_whole_not_by_trailing_digit() -> None:
    """Team 13 is not team 3, though its key ends in 3."""
    assert play_side(_event("13", 2), _mine()) is None


def test_payload_carries_the_play_plus_routing_and_side() -> None:
    payload = scoring_play_payload(_event(MINE, 2), ENTRY, LEAGUE, _mine())

    assert payload["entry_id"] == ENTRY
    assert payload["league_id"] == LEAGUE
    assert payload["side"] == "mine"
    # The existing fields are all still there.
    for key in ("event_id", "text", "player", "team_key", "matchup_id", "delta", "points", "starter", "correction"):
        assert key in payload


# --- the entity's event types -----------------------------------------------


def _type(event: ScoringEvent) -> str | None:
    return entity_event_type(scoring_play_payload(event, ENTRY, LEAGUE, _mine()))


def test_my_starter_scoring_is_my_player_scored() -> None:
    assert _type(_event(MINE, 2)) == EVENT_TYPE_MY_PLAYER_SCORED


def test_their_starter_scoring_is_opponent_scored() -> None:
    assert _type(_event("4", 2)) == EVENT_TYPE_OPPONENT_SCORED


def test_bench_points_do_not_count() -> None:
    assert _type(_event(MINE, 2, starter=False)) is None


def test_a_stat_correction_is_not_a_score() -> None:
    assert _type(_event(MINE, 2, correction=True)) is None


def test_a_negative_play_is_not_a_score() -> None:
    assert _type(_event(MINE, 2, delta=-2.0)) is None


def test_other_matchups_are_not_the_entitys_business() -> None:
    assert _type(_event("1", 1)) is None


# --- lead changes -----------------------------------------------------------


def _run(scores: list[tuple[float | None, float | None]], tracker: LeadTracker | None = None) -> list[dict]:
    tracker = tracker or LeadTracker()
    fired = []
    for mine, theirs in scores:
        change = tracker.observe(_mine(mine, theirs), ENTRY, LEAGUE)
        if change is not None:
            fired.append(change)
    return fired


def test_going_ahead_from_0_0_is_taking_the_lead() -> None:
    [change] = _run([(0, 0), (6, 0)])
    assert change["side"] == "mine"
    assert change["leader"] == "Tesla"
    assert (change["my_points"], change["opponent_points"]) == (6, 0)


def test_the_lead_changing_hands_fires_each_time() -> None:
    fired = _run([(0, 0), (6, 0), (6, 7), (13, 7)])
    assert [c["side"] for c in fired] == ["mine", "opponent", "mine"]
    assert fired[1]["leader"] == "Kush"
    assert fired[1]["trailer"] == "Tesla"


def test_extending_a_lead_is_not_a_change() -> None:
    assert len(_run([(0, 0), (6, 0), (12, 0), (12, 3)])) == 1


def test_a_tie_then_the_same_leader_is_not_a_change() -> None:
    assert len(_run([(0, 0), (6, 0), (6, 6), (9, 6)])) == 1


def test_a_tie_then_the_other_team_is_a_change() -> None:
    fired = _run([(0, 0), (6, 0), (6, 6), (6, 9)])
    assert [c["side"] for c in fired] == ["mine", "opponent"]


def test_the_first_leader_after_a_restart_is_a_baseline() -> None:
    """Mid-game, nothing says who led before HA started."""
    assert _run([(20, 14), (20, 21)]) == [_run([(0, 0), (20, 14), (20, 21)])[-1]]
    assert _run([(20, 14)]) == []


def test_a_mid_game_tie_after_a_restart_is_not_a_known_start() -> None:
    assert _run([(14, 14), (14, 17)]) == []


def test_missing_scores_change_nothing() -> None:
    assert len(_run([(0, 0), (6, 0), (None, None), (6, 3)])) == 1


def test_a_new_week_starts_a_new_baseline() -> None:
    tracker = LeadTracker()
    _run([(0, 0), (6, 0)], tracker)
    # Next week, mid-game after a restart: a baseline, not a change from last week.
    assert tracker.observe(_mine(3, 10, week=WEEK + 1), ENTRY, LEAGUE) is None


def test_no_configured_team_never_fires() -> None:
    tracker = LeadTracker()
    assert tracker.observe(None, ENTRY, LEAGUE) is None
