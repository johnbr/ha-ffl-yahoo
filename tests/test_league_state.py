"""Tests for the view layer the entities and cards consume.

These shapes are a contract with the frontend, so they are pinned here rather
than discovered when a card renders wrong. The coordinator is intentionally a
thin shell over this module, since HA machinery cannot be unit-tested in this
repo's harness.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from yahoo_fantasy_football.const import (
    SCAN_INTERVAL_IDLE_SECONDS,
    SCAN_INTERVAL_LIVE_SECONDS,
    SCAN_INTERVAL_NEAR_GAME_SECONDS,
)
from yahoo_fantasy_football.league_state import (
    ATTR_RECENT_PLAYS,
    find_team,
    matchup_rows,
    play_dict,
    player_rows,
    poll_interval,
    scoreboard_attributes,
    scoreboard_state,
    win_probability,
)
from yahoo_fantasy_football.plays import PlayFeed, diff_snapshots
from yahoo_fantasy_football.web_client import LeagueData, YahooWebClient
from yahoo_fantasy_football.yahoo_web import to_snapshot

FIXTURES = Path(__file__).parent / "fixtures"
MATCHUP = (FIXTURES / "yahoo_web_matchup_2025_w13.html").read_text(encoding="utf-8")
SCOREBOARD = (FIXTURES / "yahoo_web_scoreboard_2025_w13.html").read_text(encoding="utf-8")


async def _fetch(url: str) -> tuple[str, str]:
    return (MATCHUP if "/matchup?" in url else SCOREBOARD), url


DATA: LeagueData = asyncio.run(
    YahooWebClient(_fetch, league_id=476807, season=2025).async_refresh(now=100.0)
)


# ---------------------------------------------------------------------------
# Poll cadence
# ---------------------------------------------------------------------------


def test_a_completed_slate_drops_to_idle() -> None:
    """Every game in the fixture is final, so nothing needs fast polling."""
    assert poll_interval(DATA) == SCAN_INTERVAL_IDLE_SECONDS


def test_a_live_game_forces_live_cadence() -> None:
    from dataclasses import replace

    m = DATA.matchups[0]
    live = replace(m, players=[replace(m.players[0], game_note="Q3 5:22 vs Pit"), *m.players[1:]])
    assert poll_interval(replace(DATA, matchups=[live])) == SCAN_INTERVAL_LIVE_SECONDS


def test_unknown_game_state_polls_conservatively_not_idly() -> None:
    """Live wording is unverified, so unknown must not be mistaken for 'done'."""
    from dataclasses import replace

    m = DATA.matchups[0]
    odd = replace(m, players=[replace(m.players[0], game_note="???"), *m.players[1:]])
    assert poll_interval(replace(DATA, matchups=[odd])) == SCAN_INTERVAL_NEAR_GAME_SECONDS


def test_no_data_yet_polls_at_the_middle_cadence() -> None:
    assert poll_interval(None) == SCAN_INTERVAL_NEAR_GAME_SECONDS


# ---------------------------------------------------------------------------
# Scoreboard rows
# ---------------------------------------------------------------------------


def test_every_matchup_produces_a_row() -> None:
    rows = matchup_rows(DATA)
    assert len(rows) == 5
    assert all(r["home"]["name"] and r["away"]["name"] for r in rows)


def test_the_leader_is_identified() -> None:
    row = next(r for r in matchup_rows(DATA) if r["home"]["name"] == "Tesla")
    assert row["home"]["points"] == pytest.approx(180.67)
    assert row["away"]["points"] == pytest.approx(104.09)
    assert row["leader"] == row["home"]["team_id"]


def test_a_tie_has_no_leader() -> None:
    from dataclasses import replace

    home, away = DATA.standings[0]
    tied = replace(DATA, standings=[(home, replace(away, points=home.points))])
    assert matchup_rows(tied)[0]["leader"] is None


def test_rows_come_from_the_scoreboard_so_a_failed_roster_still_scores() -> None:
    """A matchup whose roster fetch failed must still show its score."""
    from dataclasses import replace

    degraded = replace(DATA, matchups=DATA.matchups[:2], partial=True)
    rows = matchup_rows(degraded)
    assert len(rows) == 5, "all five scores present"
    assert [r["has_roster"] for r in rows] == [True, True, False, False, False]


# ---------------------------------------------------------------------------
# Roster popup
# ---------------------------------------------------------------------------


def test_the_popup_payload_splits_starters_from_bench() -> None:
    payload = player_rows(DATA, 0)
    assert payload["matchup_id"] == "w13.m1"
    assert len(payload["sides"]) == 2
    for side in payload["sides"]:
        assert len(side["starters"]) == 9
        assert len(side["bench"]) == 5


def test_the_popup_carries_the_fields_the_card_renders() -> None:
    starter = player_rows(DATA, 0)["sides"][0]["starters"][0]
    assert set(starter) >= {
        "name",
        "slot",
        "points",
        "projected",
        "stat_line",
        "game_state",
    }
    assert starter["name"] == "Josh Allen"
    assert starter["projected"] == pytest.approx(25.38)


def test_an_out_of_range_matchup_returns_empty_rather_than_raising() -> None:
    """A card can request a matchup that failed to fetch; that is not an error."""
    assert player_rows(DATA, 99) == {"matchup_id": None, "sides": []}


# ---------------------------------------------------------------------------
# Entity payload
# ---------------------------------------------------------------------------


def test_the_state_is_low_churn() -> None:
    """The state hits the recorder on every change, so it must not be a score."""
    assert scoreboard_state(DATA) == "w13-5"
    assert scoreboard_state(None) == "unknown"


def test_attributes_carry_the_board_and_a_bounded_play_list() -> None:
    feed = PlayFeed()
    before = to_snapshot(DATA.matchups, 13, 0.0, 476807)
    after = to_snapshot(DATA.matchups, 13, 60.0, 476807)

    from dataclasses import replace

    bumped = dict(after.players)
    for key in list(bumped)[:20]:
        bumped[key] = replace(bumped[key], points=bumped[key].points + 6.0)
    from yahoo_fantasy_football.plays import LeagueSnapshot

    feed.add(diff_snapshots(before, LeagueSnapshot(13, 60.0, bumped)))

    attrs = scoreboard_attributes(DATA, feed, "476807")
    assert attrs["week"] == 13
    assert len(attrs["matchups"]) == 5
    assert attrs["last_play"] is not None
    assert len(attrs["recent_plays"]) == ATTR_RECENT_PLAYS, "bounded, not the whole feed"
    assert attrs["source"] == "web"


def test_attributes_are_safe_before_the_first_refresh() -> None:
    attrs = scoreboard_attributes(None, PlayFeed(), "476807")
    assert attrs["week"] is None
    assert attrs["matchups"] == []
    assert attrs["last_play"] is None


def test_a_play_renders_with_text_for_the_banner() -> None:
    before = to_snapshot(DATA.matchups, 13, 0.0, 476807)
    from dataclasses import replace

    from yahoo_fantasy_football.plays import LeagueSnapshot

    after = to_snapshot(DATA.matchups, 13, 60.0, 476807)
    key = next(iter(after.players))
    bumped = dict(after.players)
    bumped[key] = replace(bumped[key], points=bumped[key].points + 6.4)

    event = diff_snapshots(before, LeagueSnapshot(13, 60.0, bumped))[0]
    d = play_dict(event)
    assert d["delta"] == pytest.approx(6.4)
    assert d["text"]
    assert d["correction"] is False


# ---------------------------------------------------------------------------
# My-team lookup
# ---------------------------------------------------------------------------


def test_a_team_is_located_with_its_opponent() -> None:
    tesla = matchup_rows(DATA)[0]["home"]
    found = find_team(DATA, tesla["team_id"])
    assert found is not None
    assert found["me"]["name"] == "Tesla"
    assert found["opponent"]["name"] == "Your daddy"
    assert found["winning"] is True


def test_the_away_side_is_found_too() -> None:
    away = matchup_rows(DATA)[0]["away"]
    found = find_team(DATA, away["team_id"])
    assert found is not None
    assert found["me"]["name"] == "Your daddy"
    assert found["winning"] is False


def test_an_unconfigured_or_missing_team_is_not_an_error() -> None:
    assert find_team(DATA, None) is None
    assert find_team(DATA, "99999") is None
    assert find_team(None, "1") is None


# Per-matchup last play
# ---------------------------------------------------------------------------


def _event(event_id: str, matchup_id: str, player_name: str, delta: float, **kw):
    from yahoo_fantasy_football.plays import ScoringEvent

    return ScoringEvent(
        event_id=event_id,
        week=DATA.week,
        timestamp=kw.pop("timestamp", 1.0),
        player_key=f"p.{event_id}",
        player_name=player_name,
        team_key=kw.pop("team_key", "t.1"),
        matchup_id=matchup_id,
        nfl_team=kw.pop("nfl_team", "NE"),
        position="WR",
        delta=delta,
        old_points=0.0,
        new_points=delta,
        starter=kw.pop("starter", True),
        correction=delta < 0,
        **kw,
    )


def _mid(index: int) -> str:
    return f"w{DATA.week}.m{index}"


def test_each_row_gets_its_own_last_play() -> None:
    """Five matchups on one card means five answers to "what just happened"."""
    feed = PlayFeed()
    feed.add(
        [
            _event("e1", _mid(1), "A.J. Brown", 5.6),
            _event("e2", _mid(2), "R. Stevenson", 1.6),
            _event("e3", _mid(1), "Drake Maye", 0.5),
        ]
    )
    rows = {r["matchup_id"]: r for r in matchup_rows(DATA, feed)}

    assert rows[_mid(1)]["last_play"]["player"] == "Drake Maye", "most recent, not first"
    assert rows[_mid(2)]["last_play"]["player"] == "R. Stevenson"


def test_a_quiet_matchup_reports_no_play() -> None:
    feed = PlayFeed()
    feed.add([_event("e1", _mid(1), "A.J. Brown", 5.6)])
    rows = {r["matchup_id"]: r for r in matchup_rows(DATA, feed)}

    assert rows[_mid(2)]["last_play"] is None


def test_rows_without_a_feed_still_render() -> None:
    """``find_team`` asks for rows with no feed; it must not blow up."""
    assert all(row["last_play"] is None for row in matchup_rows(DATA))


def test_the_scoreboard_attributes_carry_the_per_row_plays() -> None:
    feed = PlayFeed()
    feed.add([_event("e1", _mid(3), "Puka Nacua", 6.4)])
    attrs = scoreboard_attributes(DATA, feed, "476807")

    row = next(r for r in attrs["matchups"] if r["matchup_id"] == _mid(3))
    assert row["last_play"]["player"] == "Puka Nacua"
    assert attrs["last_play"]["player"] == "Puka Nacua", "league banner still populated"


def test_the_scoreboard_carries_the_league_name_for_the_card_header() -> None:
    attrs = scoreboard_attributes(DATA, PlayFeed(), "476807", "Kush")
    assert attrs["league_name"] == "Kush"
    assert attrs["week"] == DATA.week


def test_the_league_name_is_present_even_before_the_first_fetch() -> None:
    """The header must not pop in a poll later than the rest of the card."""
    attrs = scoreboard_attributes(None, PlayFeed(), "476807", "Kush")
    assert attrs["league_name"] == "Kush"
    assert attrs["week"] is None


# ---------------------------------------------------------------------------
# Bench plays, and which side of a row scored
# ---------------------------------------------------------------------------


def test_a_bench_players_points_never_reach_a_row() -> None:
    """Bench points are real but do not count, so they must not read as scoring."""
    feed = PlayFeed()
    feed.add(
        [
            _event("e1", _mid(1), "A Starter", 5.6),
            _event("e2", _mid(1), "A Benchwarmer", 9.9, starter=False),
        ]
    )
    rows = {r["matchup_id"]: r for r in matchup_rows(DATA, feed)}

    assert rows[_mid(1)]["last_play"]["player"] == "A Starter", "the bench play won"


def test_a_bench_only_matchup_shows_no_play_at_all() -> None:
    feed = PlayFeed()
    feed.add([_event("e1", _mid(2), "A Benchwarmer", 9.9, starter=False)])
    rows = {r["matchup_id"]: r for r in matchup_rows(DATA, feed)}

    assert rows[_mid(2)]["last_play"] is None
    attrs = scoreboard_attributes(DATA, feed, "476807")
    assert attrs["last_play"] is None
    assert attrs["recent_plays"] == []


def test_a_play_says_which_side_of_the_row_scored_it() -> None:
    home, away = DATA.standings[0]
    feed = PlayFeed()
    feed.add(
        [
            _event("e1", _mid(1), "Home Guy", 5.6, team_key=f"476807.t.{home.team_id}"),
            _event("e2", _mid(2), "Away Guy", 5.6, team_key=f"476807.t.{DATA.standings[1][1].team_id}"),
        ]
    )
    rows = {r["matchup_id"]: r for r in matchup_rows(DATA, feed)}

    assert rows[_mid(1)]["last_play"]["side"] == "home"
    assert rows[_mid(2)]["last_play"]["side"] == "away"
    assert away  # the pair is what the row was built from


def test_an_unrecognised_team_key_leaves_the_side_unset() -> None:
    """Better an un-aligned play line than one pointed at the wrong team."""
    feed = PlayFeed()
    feed.add([_event("e1", _mid(1), "Ghost", 5.6, team_key="476807.t.999")])
    rows = {r["matchup_id"]: r for r in matchup_rows(DATA, feed)}

    assert rows[_mid(1)]["last_play"]["side"] is None


def test_the_attributes_report_live_nfl_games() -> None:
    feed = PlayFeed()
    assert scoreboard_attributes(DATA, feed, "476807")["active_games"] == DATA.active_games
    assert scoreboard_attributes(None, feed, "476807")["active_games"] == 0


# ---------------------------------------------------------------------------
# Win probability
# ---------------------------------------------------------------------------


class _Side:
    def __init__(self, live: float | None, var: float) -> None:
        self.live_projected, self.remaining_var = live, var


def test_the_favourite_is_the_team_projected_higher() -> None:
    home = win_probability(_Side(140.0, 1800.0), _Side(128.0, 1750.0))
    assert 0.5 < home < 1.0


def test_it_lands_near_yahoos_published_number() -> None:
    """Calibration check against a real matchup: Yahoo showed 41/59."""
    tesla, herb = _Side(128.12, 1824.8), _Side(139.17, 1762.2)
    assert win_probability(tesla, herb) == pytest.approx(0.41, abs=0.02)


def test_a_dead_heat_is_a_coin_flip() -> None:
    assert win_probability(_Side(130.0, 900.0), _Side(130.0, 900.0)) == 0.5


def test_a_lead_with_nothing_left_to_play_is_certain() -> None:
    """Every game final: there is no uncertainty left to model."""
    assert win_probability(_Side(140.0, 0.0), _Side(128.0, 0.0)) == 1.0
    assert win_probability(_Side(128.0, 0.0), _Side(140.0, 0.0)) == 0.0


def test_a_bigger_lead_is_a_better_chance() -> None:
    small = win_probability(_Side(132.0, 1800.0), _Side(130.0, 1800.0))
    large = win_probability(_Side(160.0, 1800.0), _Side(130.0, 1800.0))
    assert large > small


def test_the_same_lead_is_safer_with_less_football_left() -> None:
    early = win_probability(_Side(140.0, 1800.0), _Side(130.0, 1800.0))
    late = win_probability(_Side(140.0, 200.0), _Side(130.0, 200.0))
    assert late > early


def test_no_projection_means_no_probability() -> None:
    assert win_probability(_Side(None, 0.0), _Side(130.0, 900.0)) is None


# ---------------------------------------------------------------------------
# A play does not outlive its game
# ---------------------------------------------------------------------------


def _with_clubs(clubs):
    """DATA is shared and frozen-ish; hand back a copy carrying live clubs."""
    from dataclasses import replace

    return replace(DATA, live_clubs=clubs)


def test_a_play_from_a_finished_game_stops_being_shown() -> None:
    feed = PlayFeed()
    feed.add([_event("e1", _mid(1), "Done Guy", 5.6, nfl_team="NE")])
    rows = {r["matchup_id"]: r for r in matchup_rows(_with_clubs(frozenset({"LAR"})), feed)}

    assert rows[_mid(1)]["last_play"] is None


def test_it_falls_back_to_the_newest_play_from_a_game_still_running() -> None:
    """1pm games final, 4pm games live: show the 4pm play, not the stale one."""
    feed = PlayFeed()
    feed.add(
        [
            _event("e1", _mid(1), "Late Guy", 5.6, nfl_team="LAR"),
            _event("e2", _mid(1), "Early Guy", 9.9, nfl_team="NE"),
        ]
    )
    rows = {r["matchup_id"]: r for r in matchup_rows(_with_clubs(frozenset({"LAR"})), feed)}

    assert rows[_mid(1)]["last_play"]["player"] == "Late Guy", "newest LIVE play, not newest"


def test_an_unavailable_games_feed_does_not_blank_every_play() -> None:
    """``None`` is "we don't know", which is not "nothing is live"."""
    feed = PlayFeed()
    feed.add([_event("e1", _mid(1), "Some Guy", 5.6, nfl_team="NE")])
    rows = {r["matchup_id"]: r for r in matchup_rows(_with_clubs(None), feed)}

    assert rows[_mid(1)]["last_play"]["player"] == "Some Guy"


def test_a_slate_with_nothing_live_shows_no_plays() -> None:
    feed = PlayFeed()
    feed.add([_event("e1", _mid(1), "Some Guy", 5.6, nfl_team="NE")])
    rows = {r["matchup_id"]: r for r in matchup_rows(_with_clubs(frozenset()), feed)}

    assert rows[_mid(1)]["last_play"] is None


# ---------------------------------------------------------------------------
# The NFL slate payload
# ---------------------------------------------------------------------------


def _slate():
    """A real LeagueData built from the captured relay, for the games card."""
    from yahoo_fantasy_football.yahoo_redzone import league_from_payloads

    fixtures = Path(__file__).resolve().parent / "fixtures"

    return league_from_payloads(
        (fixtures / "yahoo_redzone_2026_w1.json").read_text(),
        (fixtures / "yahoo_relay_stats_2026_w1.txt").read_text(),
        (fixtures / "yahoo_relay_games_2026_w1.txt").read_text(),
        "999999",
        now=1000.0,
    )


def test_nfl_game_rows_cover_the_whole_slate() -> None:
    from yahoo_fantasy_football.league_state import nfl_game_rows

    rows = nfl_game_rows(_slate())
    assert rows, "the slate must not be empty"
    assert len({r["game_id"] for r in rows}) == len(rows), "one row per game"
    for row in rows:
        assert row["away"]["abbr"], "clubs render as names, never numeric ids"
        assert row["home"]["abbr"]
        assert row["state"] in {"pre", "in", "post", "unknown"}


def _game_row(status: str, **over):
    """A synthetic ``g|`` row.

    The captured fixture holds only a live game and fifteen scheduled ones —
    no final — so the finished-game path needs building rather than sampling.
    Column order is GAME_ROW's; see yahoo_redzone.
    """
    from yahoo_fantasy_football.yahoo_redzone import GameState

    cells = ["g", "2026091301", "17", "26", status, "0",
             over.get("period", "4"), over.get("clock", "0:00"),
             "21", "24", "1789000000",
             over.get("down", "0"), over.get("distance", "0"), "50", "0"]
    return GameState(cells)


def test_a_finished_game_says_final_and_reads_as_complete() -> None:
    from yahoo_fantasy_football.league_state import _ball_spot, _clock_text, _situation

    game = _game_row("F")
    assert game.state == "post"
    assert _clock_text(game) == "Final"
    assert _situation(game) == "", "a finished game has no down and distance"
    assert _ball_spot(game) == "", "nor a ball on the field"


def test_a_live_game_reads_its_quarter_and_down() -> None:
    from yahoo_fantasy_football.league_state import _clock_text, _situation

    game = _game_row("P", period="3", clock="6:24", down="2", distance="7")
    assert _clock_text(game) == "Q3 6:24"
    assert _situation(game) == "2nd & 7"


def test_overtime_is_not_printed_as_a_quarter_number() -> None:
    from yahoo_fantasy_football.league_state import _clock_text

    assert _clock_text(_game_row("P", period="5", clock="8:11")) == "OT 8:11"


def _spot(to_goal: str, ball: str):
    """A live game with the ball at a given distance from the goal.

    Away is team 17, home is team 26 — see ``_game_row``.
    """
    from yahoo_fantasy_football.league_state import _ball_spot
    from yahoo_fantasy_football.yahoo_redzone import GameState

    cells = ["g", "2026091301", "17", "26", "P", "0", "2", "9:07",
             "0", "0", "1789000000", "2", "7", to_goal, ball]
    return _ball_spot(GameState(cells))


def test_the_ball_spot_past_midfield_names_the_defending_club() -> None:
    """Yahoo's own rail: "2nd & 7, DAL 19" with the RZ badge on NYG.

    The Giants have the ball nineteen yards from Dallas's end zone, so the
    marker sits on Dallas's half — the number belongs to the club being
    driven on, not the one driving.
    """
    # Away (17 = NE) has the ball, 19 from the goal -> home's half (26 = Sea).
    assert _spot("19", "17") == "Sea 19"
    # And the mirror: home has it, deep in the away club's territory.
    assert _spot("8", "26") == "NE 8"


def test_the_ball_spot_in_a_club_s_own_half_counts_up_from_its_goal() -> None:
    # 81 to go means they are on their own 19.
    assert _spot("81", "17") == "NE 19"
    assert _spot("61", "26") == "Sea 39"


def test_midfield_belongs_to_nobody() -> None:
    assert _spot("50", "17") == "50"


def test_a_spot_the_feed_cannot_mean_is_not_printed() -> None:
    """0 is what the feed parks at between plays, after a score, on a kickoff."""
    assert _spot("0", "17") == ""
    assert _spot("100", "17") == ""
    assert _spot("42", "") == "", "no ball carrier, no side of the field"


def test_down_and_distance_is_dropped_when_the_spot_is_gone() -> None:
    """Observed live: the feed keeps a finished play's down and distance.

    Between a PAT and the kickoff it reported down=1 dist=3 from the snap
    before the touchdown, with yards-to-goal already zeroed.
    """
    from yahoo_fantasy_football.league_state import _situation
    from yahoo_fantasy_football.yahoo_redzone import GameState

    stale = GameState(["g", "1", "17", "26", "P", "0", "1", "6:40",
                       "0", "7", "1789000000", "1", "3", "0", "19"])
    assert _situation(stale) == "", "a play that has ended must not be captioned"

    live = GameState(["g", "1", "17", "26", "P", "0", "1", "6:40",
                      "0", "7", "1789000000", "1", "3", "44", "19"])
    assert _situation(live) == "1st & 3"


def test_goal_to_go_says_goal_rather_than_a_distance() -> None:
    from yahoo_fantasy_football.league_state import _situation

    assert _situation(_game_row("P", down="1", distance="0")) == "1st & goal"


def test_a_scheduled_game_carries_its_kickoff_and_no_clock() -> None:
    from yahoo_fantasy_football.league_state import nfl_game_rows

    rows = [r for r in nfl_game_rows(_slate()) if r["state"] == "pre"]
    assert rows, "the fixture must contain a scheduled game"
    for row in rows:
        assert row["clock_text"] == "", "the card formats kickoff in the viewer's zone"
        assert isinstance(row["start_time"], int) and row["start_time"] > 0
        assert row["ball_on"] == "", "a game that has not started has no ball spot"


def test_down_and_distance_only_appears_on_a_live_game() -> None:
    from yahoo_fantasy_football.league_state import nfl_game_rows

    for row in nfl_game_rows(_slate()):
        if row["state"] != "in":
            assert row["situation"] == ""


def test_the_slate_state_is_the_live_count() -> None:
    from yahoo_fantasy_football.league_state import nfl_games_attributes, nfl_games_state

    data = _slate()
    assert nfl_games_state(data) == str(data.active_games)
    assert nfl_games_state(None) == "unknown"

    attrs = nfl_games_attributes(data, "999999")
    assert attrs["league_id"] == "999999"
    assert attrs["total_games"] == len(attrs["games"])


def test_the_slate_payload_carries_no_play_lists() -> None:
    """~20 KB per game; they load on demand. See nfl_game_rows."""
    from yahoo_fantasy_football.league_state import nfl_game_rows

    for row in nfl_game_rows(_slate()):
        assert "plays" not in row
        assert row["plays_id"], "but each row must say where to fetch them"
