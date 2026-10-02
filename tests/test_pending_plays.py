"""The provisional play result, read off the games feed ahead of Yahoo's text."""

from __future__ import annotations

from types import SimpleNamespace

from yahoo_fantasy_football.pending_plays import (
    CHASE_FOR_SECONDS,
    Landed,
    PendingPlays,
    describe_change,
    snap_of,
)

DET, BUF = "8", "2"


def _game(team=DET, down=1, distance=10, to_goal=75, away=0, home=0, state="in", clock=""):
    """A games-feed row, Detroit at Buffalo, as ``GameState`` exposes it."""
    return SimpleNamespace(
        state=state,
        away=DET,
        home=BUF,
        team_with_ball=team,
        down=str(down),
        distance=str(distance),
        yards_to_goal=str(to_goal),
        away_score=str(away),
        home_score=str(home),
        period="2",
        clock=clock,
    )


def _ran_from(game):
    """The situation a play-feed row run from ``game`` would record."""
    return snap_of(game).situation


def _describe(before, after):
    return describe_change(snap_of(before), snap_of(after), DET, BUF)


# -- describe_change ---------------------------------------------------------


def test_a_gain_that_moves_the_chains_says_so() -> None:
    assert _describe(_game(down=2, distance=6, to_goal=66), _game(to_goal=49)) == "Gain of 17, 1st down"


def test_a_gain_short_of_the_line_is_just_the_yards() -> None:
    assert _describe(_game(), _game(down=2, distance=6, to_goal=71)) == "Gain of 4"


def test_first_and_ten_to_first_and_ten_is_a_first_down() -> None:
    assert _describe(_game(), _game(to_goal=60)) == "Gain of 15, 1st down"


def test_an_incompletion_is_no_gain() -> None:
    assert _describe(_game(down=2, distance=6, to_goal=66), _game(down=3, distance=6, to_goal=66)) == "No gain"


def test_a_sack_is_a_loss() -> None:
    assert _describe(_game(down=3, distance=6, to_goal=66), _game(down=4, distance=14, to_goal=74)) == "Loss of 8"
    # Live, 2026-10-01: the down moved, the spot had not yet.
    assert _describe(_game(down=3, distance=15, to_goal=35), _game(down=4, distance=25, to_goal=35)) == "Loss of 10"


def test_a_touchdown_is_named_for_the_club_that_scored() -> None:
    assert _describe(_game(down=2, to_goal=3), _game(to_goal=0, away=6)) == "Det touchdown"
    assert _describe(_game(down=2, to_goal=3), _game(to_goal=0, away=7)) == "Det touchdown"


def test_kicks_and_tries() -> None:
    assert _describe(_game(down=4, to_goal=20), _game(to_goal=0, away=3)) == "Det field goal"
    assert _describe(_game(to_goal=0, away=6), _game(to_goal=0, away=7)) == "Det extra point"
    assert _describe(_game(to_goal=0, away=6), _game(to_goal=0, away=8)) == "Det two-point conversion"


def test_a_safety_is_scored_by_the_defence() -> None:
    assert _describe(_game(down=2, to_goal=99), _game(to_goal=0, home=2)) == "Buf safety"


def test_losing_the_ball_before_fourth_down_is_a_turnover() -> None:
    assert _describe(_game(down=2, to_goal=60), _game(team=BUF, to_goal=55)) == "Turnover, Buf ball"
    assert _describe(_game(distance=5, to_goal=60), _game(team=BUF, to_goal=55)) == "Turnover, Buf ball"


def test_a_fourth_down_kick_that_flips_the_field_is_a_punt() -> None:
    assert _describe(_game(down=4, distance=9, to_goal=70), _game(team=BUF, to_goal=80)) == "Punt, Buf ball"


def test_a_fourth_down_stop_is_only_a_change_of_possession() -> None:
    after = _game(team=BUF, to_goal=62)
    assert _describe(_game(down=4, distance=1, to_goal=38), after) == "Change of possession, Buf ball"


def test_nothing_is_said_without_a_ball_on_the_field() -> None:
    # Kickoff: the spot is parked at 0 until the return is down.
    assert _describe(_game(to_goal=0, away=7), _game(team=BUF, to_goal=75, away=7)) == ""
    # Nobody has the ball at half time.
    assert _describe(_game(to_goal=40), _game(team="0", to_goal=40)) == ""


# -- PendingPlays -------------------------------------------------------------


def _tracker(game, newest=None, now=0.0) -> PendingPlays:
    pending = PendingPlays()
    pending.observe("2", game, newest, now)
    return pending


def test_the_first_look_is_a_baseline_with_nothing_to_say() -> None:
    assert _tracker(_game()).texts() == {}


def test_a_snap_shows_its_result_until_its_text_lands() -> None:
    start = _game(down=2, distance=6, to_goal=66)
    pending = _tracker(start, newest=Landed(40, ("", 0, 0, 0)))
    pending.observe("2", _game(to_goal=49), Landed(40, ("", 0, 0, 0)), 30.0)
    assert pending.text("2") == "Gain of 17, 1st down"
    assert pending.awaiting(33.0) == ["2"]

    pending.observe("2", _game(to_goal=49), Landed(41, _ran_from(start)), 45.0)
    assert pending.text("2") == ""
    assert pending.awaiting(48.0) == []


def test_a_spot_that_moves_ahead_of_the_down_waits_for_it() -> None:
    pending = _tracker(_game(down=2, distance=6, to_goal=66))
    pending.observe("2", _game(down=2, distance=6, to_goal=49), None, 30.0)
    assert pending.text("2") == ""  # could as well be a re-spot
    pending.observe("2", _game(to_goal=49), None, 40.0)
    assert pending.text("2") == "Gain of 17, 1st down"


def test_a_down_that_moves_ahead_of_the_spot_is_read_from_the_distance() -> None:
    """The order seen live on 2026-10-01, Cle at their own 20 and 28."""
    pending = _tracker(_game(team=BUF, to_goal=80))
    pending.observe("2", _game(team=BUF, down=2, distance=6, to_goal=80), None, 30.0)
    assert pending.text("2") == "Gain of 4"
    pending.observe("2", _game(team=BUF, down=2, distance=5, to_goal=75), None, 42.0)
    assert pending.text("2") == "Gain of 5"

    pending = _tracker(_game(team=BUF, down=3, distance=2, to_goal=72))
    pending.observe("2", _game(team=BUF, to_goal=72), None, 30.0)
    assert pending.text("2") == "1st down"
    pending.observe("2", _game(team=BUF, to_goal=65), None, 42.0)
    assert pending.text("2") == "Gain of 7, 1st down"


def test_a_re_spot_is_not_a_play() -> None:
    """Seen live at the end of a quarter: 3rd & 3 at the 73, then at the 72."""
    assert _describe(_game(down=3, distance=3, to_goal=73), _game(down=3, distance=3, to_goal=72)) == ""
    assert _describe(_game(to_goal=75), _game(to_goal=74)) == ""


def test_a_penalty_replaying_the_down_is_read_from_the_distance() -> None:
    assert _describe(_game(), _game(distance=15, to_goal=75)) == "Loss of 5"


def test_a_play_that_is_undone_takes_its_provisional_line_with_it() -> None:
    """Seen live: Cle lost the ball, then had it back where it was (a flag)."""
    start = _game(team=BUF, to_goal=65)
    pending = _tracker(start)
    pending.observe("2", _game(team=DET, to_goal=76), None, 30.0)
    assert pending.text("2") == "Change of possession, Det ball"
    pending.observe("2", start, None, 80.0)
    assert pending.text("2") == ""
    # The penalty's row is the awaited play, run from where it all started.
    pending.observe("2", start, Landed(41, _ran_from(start)), 86.0)
    pending.observe("2", _game(team=BUF, to_goal=23), Landed(41, _ran_from(start)), 92.0)
    assert pending.text("2") == ""


def test_a_touchdown_whose_score_trails_its_spot_is_still_called() -> None:
    pending = _tracker(_game(down=2, distance=3, to_goal=3))
    pending.observe("2", _game(down=2, distance=3, to_goal=0), None, 30.0)
    assert pending.text("2") == ""  # a parked spot alone says nothing
    pending.observe("2", _game(down=2, distance=3, to_goal=0, away=6), None, 45.0)
    assert pending.text("2") == "Det touchdown"


def test_no_provisional_when_the_text_beat_the_games_feed() -> None:
    start = _game(down=2, distance=6, to_goal=66)
    pending = _tracker(start, newest=Landed(40, ("", 0, 0, 0)))
    pending.observe("2", _game(to_goal=49), Landed(41, _ran_from(start)), 30.0)
    assert pending.texts() == {}


def test_a_play_landing_with_a_games_feed_change_is_taken_as_its_text() -> None:
    """Live 2026-10-01: a kickoff's catch, then its return, then its text."""
    pit_kicks = _game(team=DET, to_goal=65, clock="10:10")
    pending = _tracker(pit_kicks, newest=Landed(139, ("0", 0, 0, 0), "2", "10:10"))
    caught = _game(team=BUF, to_goal=99, clock="10:08")
    pending.observe("2", caught, Landed(139, ("0", 0, 0, 0), "2", "10:10"), 150.0)
    pending.observe("2", _game(team=BUF, to_goal=66, clock="10:04"), Landed(140, ("0", 0, 0, 0), "2", "10:10"), 176.0)
    assert pending.text("2") == ""


def test_two_snaps_awaiting_text_wait_for_the_second() -> None:
    first = _game(down=2, distance=3, to_goal=3)
    touchdown = _game(down=2, distance=3, to_goal=0, away=6)
    pending = _tracker(first, newest=Landed(40, ("", 0, 0, 0)))
    pending.observe("2", touchdown, None, 30.0)
    pending.observe("2", _game(down=2, distance=3, to_goal=0, away=7), None, 60.0)
    assert pending.text("2") == "Det extra point"

    # The touchdown's text lands: the try is still on its way.
    pending.observe("2", _game(down=2, distance=3, to_goal=0, away=7), Landed(41, _ran_from(first)), 65.0)
    assert pending.text("2") == "Det extra point"
    # The try's row lands.
    pending.observe("2", _game(down=2, distance=3, to_goal=0, away=7), Landed(42, ("8", 0, 0, 0)), 70.0)
    assert pending.text("2") == ""


def test_a_re_read_of_the_play_feed_alone_clears_on_landing() -> None:
    """The chase between polls hands the SAME games-feed row back."""
    start = _game(down=3, distance=6, to_goal=66)
    after = _game(down=4, distance=6, to_goal=66)
    pending = _tracker(start, newest=Landed(40, ("", 0, 0, 0)))
    pending.observe("2", after, Landed(40, ("", 0, 0, 0)), 30.0)
    pending.observe("2", after, Landed(40, ("", 0, 0, 0)), 33.0)
    assert pending.text("2") == "No gain"
    pending.observe("2", after, Landed(41, _ran_from(start)), 36.0)
    assert pending.text("2") == ""


def test_the_chase_gives_up_but_the_line_stays() -> None:
    pending = _tracker(_game())
    pending.observe("2", _game(down=2, distance=6, to_goal=71), None, 30.0)
    assert pending.awaiting(30.0 + CHASE_FOR_SECONDS) == []
    assert pending.text("2") == "Gain of 4"


def test_a_game_that_stops_being_live_is_forgotten() -> None:
    pending = _tracker(_game())
    pending.observe("2", _game(down=2, distance=6, to_goal=71), None, 30.0)
    pending.observe("2", _game(down=2, distance=6, to_goal=71, state="post"), None, 40.0)
    assert pending.texts() == {}

    pending = _tracker(_game())
    pending.observe("2", _game(down=2, distance=6, to_goal=71), None, 30.0)
    pending.forget_others(set())
    assert pending.texts() == {}


def test_the_list_row_carries_the_down_it_was_run_from() -> None:
    pending = _tracker(_game(down=3, distance=6, to_goal=66))
    after = _game(down=4, distance=14, to_goal=74)
    pending.observe("2", after, None, 30.0)
    row = pending.row("2", after)
    assert row == {
        "play_id": "",
        "text": "Loss of 8",
        "short_text": "Loss of 8",
        "situation": "3rd & 6",
        "period": "2",
        "clock": "",
        "provisional": True,
    }
    assert _tracker(_game()).row("2", _game()) is None


def test_the_games_card_row_carries_the_provisional_line() -> None:
    from yahoo_fantasy_football.league_state import nfl_game_rows

    game = _game()
    game.game_id, game.plays_id, game.clock, game.start_time, game.quarters = "g", "2", "9:00", 0, {}
    game.has_ball = lambda club: club == DET
    game.in_red_zone = lambda club: False
    data = SimpleNamespace(nfl_games=[game])

    (row,) = nfl_game_rows(data, {"2": "J. Goff passed to J. Gibbs"}, {"2": "Gain of 7"})
    assert (row["last_play"], row["last_play_provisional"]) == ("Gain of 7", True)
    (row,) = nfl_game_rows(data, {"2": "J. Goff passed to J. Gibbs"})
    assert (row["last_play"], row["last_play_provisional"]) == ("J. Goff passed to J. Gibbs", False)


def test_a_play_snapped_after_its_origin_appeared_is_its_text_whatever_the_row_says() -> None:
    """Live 2026-10-01: after a turnover the next play's row named the OTHER
    club's last spot as where it ran from, and landed in the same poll as the
    games feed showing its result. The clock is what ties it to its snap."""
    cle = _game(team=BUF, to_goal=23, clock="14:11")
    pending = _tracker(cle, newest=Landed(131, ("", 0, 0, 0), "2", "14:19"))
    pending.observe("2", _game(team=DET, to_goal=77, clock="14:11"), Landed(131, ("", 0, 0, 0), "2", "14:19"), 25.0)
    assert pending.text("2") == "Change of possession, Det ball"

    after = _game(team=DET, down=2, distance=3, to_goal=70, clock="13:54")
    pending.observe("2", after, Landed(132, _ran_from(cle), "2", "14:11"), 62.0)
    assert pending.text("2") == ""


def test_a_play_that_lands_with_nothing_awaited_is_the_next_changes_text() -> None:
    """Live 2026-10-01: the kickoff's text landed 3 s before the return's spot."""
    kicked = _game(team=BUF, to_goal=99, clock="10:04")
    pending = _tracker(kicked, newest=Landed(139, ("0", 0, 0, 0), "2", "10:10"))
    kickoff = Landed(140, ("0", 0, 0, 0), "2", "10:10")
    pending.observe("2", kicked, kickoff, 160.0)
    pending.observe("2", _game(team=BUF, to_goal=66, clock="10:04"), kickoff, 163.0)
    assert pending.text("2") == ""
    # And it is spent: the next snap is provisional again.
    pending.observe("2", _game(team=BUF, down=2, distance=15, to_goal=70, clock="9:50"), kickoff, 190.0)
    assert pending.text("2") == "Loss of 5"


def test_a_timeout_landing_does_not_stand_in_for_the_next_snaps_text() -> None:
    start = _game(down=2, distance=6, to_goal=66)
    pending = _tracker(start, newest=Landed(40, ("", 0, 0, 0)))
    timeout = Landed(41, ("0", 0, 0, 0), "2", "9:40", "24")
    pending.observe("2", start, timeout, 30.0)
    pending.observe("2", _game(down=3, distance=6, to_goal=66), timeout, 60.0)
    assert pending.text("2") == "No gain"
    # Nor when it lands in the same poll.
    pending = _tracker(start, newest=Landed(40, ("", 0, 0, 0)))
    pending.observe("2", _game(down=3, distance=6, to_goal=66), timeout, 30.0)
    assert pending.text("2") == "No gain"


def test_a_kickoff_is_only_a_change_of_possession() -> None:
    assert _describe(_game(to_goal=65), _game(team=BUF, to_goal=99)) == "Change of possession, Buf ball"
