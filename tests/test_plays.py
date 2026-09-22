"""Tests for the scoring-play engine.

The engine is a pure function over two consecutive polls, so every case is
reachable with hand-built snapshots — no network, no Home Assistant, no clock.

Most of these tests are about what must NOT produce an event. Synthesizing a
feed by diffing totals means several ordinary situations look exactly like
scoring unless they are explicitly handled: a restart, a week rollover, a
waiver pickup arriving with points already on the board.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from yahoo_fantasy_football.espn import match_play_to_player, parse_scoring_play
from yahoo_fantasy_football.plays import (
    LeagueSnapshot,
    MatchedPlay,
    PlayerSnapshot,
    PlayFeed,
    abbreviate_name,
    describe,
    diff_snapshots,
    dumps,
    enrich_events,
    loads,
    match_relay_play,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def player(key: str, points: float, **kw) -> PlayerSnapshot:
    return PlayerSnapshot(
        player_key=key,
        name=kw.pop("name", "Puka Nacua"),
        points=points,
        team_key=kw.pop("team_key", "461.l.1.t.1"),
        matchup_id=kw.pop("matchup_id", "m1"),
        nfl_team=kw.pop("nfl_team", "LAR"),
        position=kw.pop("position", "WR"),
        selected_position=kw.pop("selected_position", "WR"),
        **kw,
    )


def snap(week: int, taken_at: float, *players: PlayerSnapshot) -> LeagueSnapshot:
    return LeagueSnapshot(week=week, taken_at=taken_at, players={p.player_key: p for p in players})


# ---------------------------------------------------------------------------
# The ordinary case
# ---------------------------------------------------------------------------


def test_a_player_scoring_emits_one_event() -> None:
    before = snap(3, 100.0, player("p1", 12.5))
    after = snap(3, 145.0, player("p1", 18.9))

    (event,) = diff_snapshots(before, after)
    assert event.player_key == "p1"
    assert event.delta == pytest.approx(6.4)
    assert event.old_points == pytest.approx(12.5)
    assert event.new_points == pytest.approx(18.9)
    assert event.correction is False
    assert event.starter is True
    assert event.timestamp == 145.0
    assert event.week == 3


def test_float_noise_does_not_leak_into_the_delta() -> None:
    """18.9 - 12.5 is 6.399999999999999 in binary floating point."""
    before = snap(3, 100.0, player("p1", 12.5))
    after = snap(3, 145.0, player("p1", 18.9))
    (event,) = diff_snapshots(before, after)
    assert str(event.delta) == "6.4"


def test_events_sort_biggest_first_with_corrections_last() -> None:
    before = snap(
        3, 100.0, player("p1", 10.0), player("p2", 10.0, name="Sam Darnold"), player("p3", 10.0, name="Cam Little")
    )
    after = snap(
        3, 145.0, player("p1", 12.0), player("p2", 18.0, name="Sam Darnold"), player("p3", 8.0, name="Cam Little")
    )
    events = diff_snapshots(before, after)
    assert [e.player_key for e in events] == ["p2", "p1", "p3"]
    assert events[-1].correction is True


# ---------------------------------------------------------------------------
# Things that look like scoring but are not
# ---------------------------------------------------------------------------


def test_no_previous_snapshot_establishes_a_baseline_silently() -> None:
    """The first poll after a restart must not replay the whole week."""
    assert diff_snapshots(None, snap(3, 100.0, player("p1", 22.4))) == []


def test_week_rollover_emits_nothing() -> None:
    """Points reset to zero, so every player would read as a huge correction."""
    before = snap(3, 100.0, player("p1", 22.4), player("p2", 15.0, name="Sam Darnold"))
    after = snap(4, 200.0, player("p1", 0.0), player("p2", 0.0, name="Sam Darnold"))
    assert diff_snapshots(before, after) == []


def test_a_player_added_mid_week_is_not_a_scoring_play() -> None:
    """A waiver pickup arrives carrying points already scored."""
    before = snap(3, 100.0, player("p1", 12.5))
    after = snap(3, 145.0, player("p1", 12.5), player("p9", 8.4, name="Jaylin Lane"))
    assert diff_snapshots(before, after) == []


def test_a_dropped_player_is_not_a_scoring_play() -> None:
    before = snap(3, 100.0, player("p1", 12.5), player("p9", 8.4, name="Jaylin Lane"))
    after = snap(3, 145.0, player("p1", 12.5))
    assert diff_snapshots(before, after) == []


def test_a_lineup_change_alone_emits_nothing() -> None:
    """Benching a player does not change that player's points."""
    before = snap(3, 100.0, player("p1", 12.5, selected_position="WR"))
    after = snap(3, 145.0, player("p1", 12.5, selected_position="BN"))
    assert diff_snapshots(before, after) == []


def test_starting_a_benched_player_flags_the_next_event_as_a_starter() -> None:
    before = snap(3, 100.0, player("p1", 12.5, selected_position="BN"))
    after = snap(3, 145.0, player("p1", 18.9, selected_position="WR"))
    (event,) = diff_snapshots(before, after)
    assert event.starter is True


def test_bench_points_still_emit_but_are_flagged_as_bench() -> None:
    before = snap(3, 100.0, player("p1", 12.5, selected_position="BN"))
    after = snap(3, 145.0, player("p1", 18.9, selected_position="BN"))
    (event,) = diff_snapshots(before, after)
    assert event.starter is False


def test_sub_threshold_jitter_is_ignored() -> None:
    before = snap(3, 100.0, player("p1", 12.50))
    after = snap(3, 145.0, player("p1", 12.53))
    assert diff_snapshots(before, after) == []


def test_missing_points_are_skipped_not_treated_as_zero() -> None:
    before = snap(3, 100.0, PlayerSnapshot("p1", "Puka Nacua", None, "t1"))
    after = snap(3, 145.0, player("p1", 6.4))
    assert diff_snapshots(before, after) == []


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def test_a_negative_delta_with_no_stat_line_is_a_correction() -> None:
    """The HTML tier carries no stats, so a drop can only be read as a revision."""
    before = snap(3, 100.0, player("p1", 18.9))
    after = snap(3, 145.0, player("p1", 18.6))
    (event,) = diff_snapshots(before, after)
    assert event.correction is True
    assert event.delta == pytest.approx(-0.3)


def test_an_interception_is_a_play_not_a_correction() -> None:
    """Herbert throwing a pick costs points and IS the news.

    Flagging every point drop as a correction hid it: corrections are filtered
    out of the card by default, so a real negative play silently vanished.
    """
    before = snap(3, 100.0, player("p1", 18.9, stats={"passingYards": 200.0}))
    after = snap(
        3, 145.0,
        player("p1", 16.9, stats={"passingYards": 200.0, "passingInterceptions": 1.0}),
    )
    (event,) = diff_snapshots(before, after)
    assert event.correction is False, "an interception is a play, not a data revision"
    assert event.delta == pytest.approx(-2.0)
    assert "Int" in event.stat_delta


def test_a_rush_for_a_loss_is_a_play() -> None:
    """Yards went down but an attempt went UP, so something happened."""
    before = snap(3, 100.0, player("p1", 10.0, stats={"rushingAttempts": 5.0, "rushingYards": 40.0}))
    after = snap(3, 145.0, player("p1", 9.7, stats={"rushingAttempts": 6.0, "rushingYards": 37.0}))
    (event,) = diff_snapshots(before, after)
    assert event.correction is False


def test_a_lost_fumble_is_a_play() -> None:
    before = snap(3, 100.0, player("p1", 12.0, stats={"rushingAttempts": 4.0, "rushingYards": 30.0}))
    after = snap(
        3, 145.0,
        player("p1", 10.0, stats={"rushingAttempts": 4.0, "rushingYards": 30.0,
                                  "fumbles": 1.0, "fumblesLost": 1.0}),
    )
    (event,) = diff_snapshots(before, after)
    assert event.correction is False
    assert "Fum Lost" in event.stat_delta


def test_a_sacked_quarterback_is_a_play() -> None:
    """``sacked`` is carried per-QB by the relay, verified against the fixture."""
    before = snap(3, 100.0, player("p1", 20.0, stats={"passingYards": 250.0, "sacked": 1.0}))
    after = snap(3, 145.0, player("p1", 19.6, stats={"passingYards": 246.0, "sacked": 2.0}))
    (event,) = diff_snapshots(before, after)
    assert event.correction is False


def test_every_negative_play_type_survives_the_correction_filter() -> None:
    """One table covering what a manager actually wants to see go wrong.

    Each of these costs points, and each adds a counter — which is the whole
    basis for telling them apart from Yahoo walking a number back.
    """
    cases = {
        "interception": ({"passingYards": 200.0}, {"passingYards": 200.0, "passingInterceptions": 1.0}),
        "lost fumble": ({"receptions": 3.0}, {"receptions": 3.0, "fumblesLost": 1.0}),
        "sack": ({"sacked": 0.0}, {"sacked": 1.0}),
        "rush for a loss": ({"rushingAttempts": 3.0, "rushingYards": 20.0},
                            {"rushingAttempts": 4.0, "rushingYards": 17.0}),
        "reception for a loss": ({"receptions": 2.0, "receptionYards": 18.0},
                                 {"receptions": 3.0, "receptionYards": 15.0}),
    }
    for label, (before_stats, after_stats) in cases.items():
        before = snap(3, 100.0, player("p1", 20.0, stats=before_stats))
        after = snap(3, 145.0, player("p1", 19.0, stats=after_stats))
        (event,) = diff_snapshots(before, after)
        assert event.correction is False, f"{label} must reach the card"

        feed = PlayFeed()
        feed.add([event])
        assert feed.last_play() is not None, f"{label} was filtered out as a correction"


def test_a_stat_walked_back_is_still_a_correction() -> None:
    """Nothing increased — Yahoo is unwinding something it already counted."""
    before = snap(3, 100.0, player("p1", 18.9, stats={"receptions": 3.0, "receptionYards": 40.0}))
    after = snap(3, 145.0, player("p1", 17.4, stats={"receptions": 2.0, "receptionYards": 25.0}))
    (event,) = diff_snapshots(before, after)
    assert event.correction is True


def test_an_interception_reaches_the_card() -> None:
    """The end-to-end symptom: it must survive the default correction filter."""
    before = snap(3, 100.0, player("p1", 18.9, stats={"passingYards": 200.0}))
    after = snap(
        3, 145.0,
        player("p1", 16.9, stats={"passingYards": 200.0, "passingInterceptions": 1.0}),
    )
    feed = PlayFeed()
    feed.add(diff_snapshots(before, after))
    last = feed.last_play()          # no include_corrections
    assert last is not None, "the interception must not be filtered out as a correction"
    assert last.delta == pytest.approx(-2.0)


def test_corrections_are_described_as_corrections() -> None:
    before = snap(3, 100.0, player("p1", 18.9))
    after = snap(3, 145.0, player("p1", 18.6))
    (event,) = diff_snapshots(before, after)
    assert describe(event) == "P. Nacua -0.30 (stat correction)"


# ---------------------------------------------------------------------------
# The undecomposable case
# ---------------------------------------------------------------------------


def test_two_scores_in_one_interval_produce_one_combined_event() -> None:
    """Yahoo shows one larger total; the numbers alone cannot separate them.

    This is a documented limitation, not a bug — see ``enrich_events`` for how
    both play descriptions still reach the UI.
    """
    before = snap(3, 100.0, player("p1", 6.0))
    after = snap(3, 145.0, player("p1", 18.4))
    (event,) = diff_snapshots(before, after)
    assert event.delta == pytest.approx(12.4)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_re_ingesting_the_same_transition_is_idempotent() -> None:
    """A restart restores the previous snapshot and re-diffs the same pair."""
    before = snap(3, 100.0, player("p1", 12.5))
    after = snap(3, 145.0, player("p1", 18.9))

    feed = PlayFeed()
    assert len(feed.add(diff_snapshots(before, after))) == 1
    # Same transition, different wall-clock time on the later poll.
    again = snap(3, 999.0, player("p1", 18.9))
    assert feed.add(diff_snapshots(before, again)) == []
    assert len(feed) == 1


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


def _espn_play(text: str, team: str, **kw) -> dict:
    return {
        "id": kw.pop("id", "x1"),
        "text": text,
        "team": team,
        "period": kw.pop("period", 3),
        "clock": kw.pop("clock", "8:12"),
        "credits": parse_scoring_play(text),
    }


REAL_PASS_TD = "Bijan Robinson 50 Yd pass from Michael Penix Jr. (Younghoe Koo Kick)"


def test_enrichment_attaches_a_real_play_description() -> None:
    before = snap(3, 100.0, player("p1", 6.0, name="Bijan Robinson", nfl_team="ATL", position="RB"))
    after = snap(3, 145.0, player("p1", 12.4, name="Bijan Robinson", nfl_team="ATL", position="RB"))
    events = diff_snapshots(before, after)

    (event,) = enrich_events(events, [_espn_play(REAL_PASS_TD, "ATL")], match_play_to_player)
    assert event.enriched
    assert event.best_play.text == REAL_PASS_TD
    assert event.best_play.role == "receiving_td"
    assert describe(event) == REAL_PASS_TD


def test_one_play_enriches_every_player_it_credits() -> None:
    """A passing TD credits receiver, passer and kicker — all three events."""
    args = {"nfl_team": "ATL", "matchup_id": "m1"}
    before = snap(
        3,
        100.0,
        player("p1", 0.0, name="Bijan Robinson", position="RB", **args),
        player("p2", 0.0, name="Michael Penix", position="QB", **args),
        player("p3", 0.0, name="Younghoe Koo", position="K", **args),
    )
    after = snap(
        3,
        145.0,
        player("p1", 6.0, name="Bijan Robinson", position="RB", **args),
        player("p2", 4.0, name="Michael Penix", position="QB", **args),
        player("p3", 1.0, name="Younghoe Koo", position="K", **args),
    )
    events = enrich_events(diff_snapshots(before, after), [_espn_play(REAL_PASS_TD, "ATL")], match_play_to_player)
    assert all(e.enriched for e in events)
    roles = {e.player_name: e.best_play.role for e in events}
    assert roles == {
        "Bijan Robinson": "receiving_td",
        "Michael Penix": "passing_td",
        "Younghoe Koo": "pat",
    }


def test_a_double_score_collects_both_plays_on_the_one_event() -> None:
    """The combined-delta event still shows both descriptions."""
    before = snap(3, 100.0, player("p1", 0.0, name="Bijan Robinson", nfl_team="ATL", position="RB"))
    after = snap(3, 145.0, player("p1", 12.0, name="Bijan Robinson", nfl_team="ATL", position="RB"))
    plays = [
        _espn_play(REAL_PASS_TD, "ATL", id="a", period=2, clock="10:00"),
        _espn_play("Bijan Robinson 3 Yd Rush (Younghoe Koo Kick)", "ATL", id="b", period=3, clock="1:00"),
    ]
    (event,) = enrich_events(diff_snapshots(before, after), plays, match_play_to_player)
    assert len(event.plays) == 2
    # The banner shows the most recent of the matched set.
    assert event.best_play.play_id == "b"


def test_a_play_by_another_team_is_not_attached() -> None:
    before = snap(3, 100.0, player("p1", 0.0, name="Bijan Robinson", nfl_team="TB", position="RB"))
    after = snap(3, 145.0, player("p1", 6.0, name="Bijan Robinson", nfl_team="TB", position="RB"))
    (event,) = enrich_events(diff_snapshots(before, after), [_espn_play(REAL_PASS_TD, "ATL")], match_play_to_player)
    assert not event.enriched


def test_corrections_are_never_enriched() -> None:
    """A stat revision did not happen on the field."""
    before = snap(3, 100.0, player("p1", 12.0, name="Bijan Robinson", nfl_team="ATL", position="RB"))
    after = snap(3, 145.0, player("p1", 6.0, name="Bijan Robinson", nfl_team="ATL", position="RB"))
    (event,) = enrich_events(diff_snapshots(before, after), [_espn_play(REAL_PASS_TD, "ATL")], match_play_to_player)
    assert event.correction
    assert not event.enriched


def test_enrichment_without_plays_returns_events_unchanged() -> None:
    before = snap(3, 100.0, player("p1", 6.0))
    after = snap(3, 145.0, player("p1", 12.4))
    events = diff_snapshots(before, after)
    assert enrich_events(events, [], match_play_to_player) == events


def test_unenriched_event_falls_back_to_the_point_delta() -> None:
    before = snap(3, 100.0, player("p1", 12.5))
    after = snap(3, 145.0, player("p1", 18.9))
    (event,) = diff_snapshots(before, after)
    assert describe(event) == "P. Nacua +6.40"


# ---------------------------------------------------------------------------
# Feed behaviour
# ---------------------------------------------------------------------------


def test_feed_returns_newest_first() -> None:
    feed = PlayFeed()
    for i in range(3):
        before = snap(3, 100.0 + i, player("p1", float(i)))
        after = snap(3, 101.0 + i, player("p1", float(i) + 5))
        feed.add(diff_snapshots(before, after))
    assert [e.new_points for e in feed.recent(3)] == [7.0, 6.0, 5.0]


def test_feed_evicts_old_events_and_can_still_record() -> None:
    """Eviction must release the dedupe id, or the feed silently goes deaf."""
    feed = PlayFeed(maxlen=2)
    for i in range(5):
        before = snap(3, 100.0 + i, player("p1", float(i)))
        after = snap(3, 101.0 + i, player("p1", float(i) + 5))
        assert len(feed.add(diff_snapshots(before, after))) == 1
    assert len(feed) == 2


def test_feed_filters_by_matchup_and_team() -> None:
    before = snap(
        3,
        100.0,
        player("p1", 0.0, matchup_id="m1", team_key="t1"),
        player("p2", 0.0, name="Sam Darnold", matchup_id="m2", team_key="t2"),
    )
    after = snap(
        3,
        145.0,
        player("p1", 6.0, matchup_id="m1", team_key="t1"),
        player("p2", 6.0, name="Sam Darnold", matchup_id="m2", team_key="t2"),
    )
    feed = PlayFeed()
    feed.add(diff_snapshots(before, after))
    assert [e.player_key for e in feed.recent(matchup_id="m1")] == ["p1"]
    assert [e.player_key for e in feed.recent(team_key="t2")] == ["p2"]


def test_banner_skips_corrections_by_default() -> None:
    feed = PlayFeed()
    feed.add(diff_snapshots(snap(3, 1.0, player("p1", 0.0)), snap(3, 2.0, player("p1", 6.0))))
    feed.add(diff_snapshots(snap(3, 3.0, player("p1", 6.0)), snap(3, 4.0, player("p1", 5.7))))
    assert feed.last_play().delta == pytest.approx(6.0)
    assert feed.last_play(include_corrections=True).correction is True


def test_a_new_week_clears_the_feed() -> None:
    feed = PlayFeed()
    feed.add(diff_snapshots(snap(3, 1.0, player("p1", 0.0)), snap(3, 2.0, player("p1", 6.0))))
    assert len(feed) == 1
    feed.add(diff_snapshots(snap(4, 3.0, player("p1", 0.0)), snap(4, 4.0, player("p1", 3.0))))
    assert len(feed) == 1
    assert feed.week == 4


def test_empty_feed_has_no_last_play() -> None:
    assert PlayFeed().last_play() is None


def test_start_week_clears_an_older_week_exactly_once() -> None:
    """The regression of 2026-09-17: a feed restored with week 1 met week 2.

    The coordinator cleared it, but the feed's week stayed at 1 — only ``add``
    moved it, and ``add`` never ran because the coordinator also reset its
    baseline on every "new week", so no diff could ever produce an event.
    Every poll of the first game of week 2 was the first poll.
    """
    feed = PlayFeed.from_dict(
        PlayFeed().to_dict() | {"week": 1}
    )
    feed.add(diff_snapshots(snap(1, 1.0, player("p1", 0.0)), snap(1, 2.0, player("p1", 6.0))))
    assert (len(feed), feed.week) == (1, 1)

    assert feed.start_week(2) is True, "an older week's history is cleared"
    assert (len(feed), feed.week) == (0, 2)
    assert feed.start_week(2) is False, "the next poll of the same week is not a rollover"

    feed.add(diff_snapshots(snap(2, 3.0, player("p1", 0.0)), snap(2, 4.0, player("p1", 3.0))))
    assert len(feed) == 1
    assert feed.start_week(2) is False
    assert len(feed) == 1, "a poll must not wipe the week's own events"


def test_start_week_on_a_fresh_feed_is_not_a_rollover() -> None:
    feed = PlayFeed()
    assert feed.start_week(2) is False
    assert feed.week == 2
    assert feed.start_week(None) is False, "no week yet is not a reason to clear"
    assert feed.week == 2


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_feed_round_trips_through_storage() -> None:
    before = snap(3, 100.0, player("p1", 0.0, name="Bijan Robinson", nfl_team="ATL", position="RB"))
    after = snap(3, 145.0, player("p1", 6.0, name="Bijan Robinson", nfl_team="ATL", position="RB"))
    feed = PlayFeed()
    feed.add(enrich_events(diff_snapshots(before, after), [_espn_play(REAL_PASS_TD, "ATL")], match_play_to_player))

    restored = loads(dumps(feed))
    assert len(restored) == 1
    event = restored.recent(1)[0]
    assert event.player_name == "Bijan Robinson"
    assert event.best_play.text == REAL_PASS_TD
    assert restored.week == 3


def test_restoring_history_drops_the_tackler_it_was_saved_with() -> None:
    """The store holds text humanised before the tackler rule existed, and a
    week of it survives a restart. Live on 2026-09-14 the Wallyworld row's
    expanded list showed a line that was nothing but who tackled Waddle."""
    from dataclasses import replace

    from yahoo_fantasy_football.plays import MatchedPlay, describe

    before = snap(1, 100.0, player("p1", 0.0, name="Jaylen Waddle", nfl_team="Mia", position="WR"))
    after = snap(1, 145.0, player("p1", 1.2, name="Jaylen Waddle", nfl_team="Mia", position="WR"))
    tackled = replace(
        diff_snapshots(before, after)[0],
        stat_delta="1 Rec, 2 Rec Yds",
        plays=(MatchedPlay(text="tackled by L'Jarius Sneed", role="", confidence=1.0),),
    )
    with_gain = replace(
        tackled,
        event_id="other",
        plays=(MatchedPlay(text="Tua Tagovailoa passed to Jaylen Waddle for 2 yard gain, tackled by L'Jarius Sneed",
                           role="", confidence=1.0),),
    )
    feed = PlayFeed()
    feed.add([tackled, with_gain])

    restored = {e.event_id: e for e in loads(dumps(feed)).recent(5)}
    # Nothing but the tackle: the play is gone and the stat line stands in.
    assert restored[tackled.event_id].best_play is None
    assert describe(restored[tackled.event_id]) == "J. Waddle 1 Rec, 2 Rec Yds"
    # A real play keeps everything before the tackler.
    assert describe(restored["other"]) == "Tua Tagovailoa passed to Jaylen Waddle for 2 yard gain"


def test_restored_feed_still_deduplicates() -> None:
    before = snap(3, 100.0, player("p1", 12.5))
    after = snap(3, 145.0, player("p1", 18.9))
    feed = PlayFeed()
    events = diff_snapshots(before, after)
    feed.add(events)

    restored = loads(dumps(feed))
    assert restored.add(events) == []


def test_unreadable_storage_yields_an_empty_feed() -> None:
    assert len(loads(None)) == 0
    assert len(loads("")) == 0
    assert len(loads("{not json")) == 0
    assert len(loads(json.dumps({"week": 3, "events": [{"garbage": True}]}))) == 0


def test_stored_payload_is_json_serialisable() -> None:
    feed = PlayFeed()
    feed.add(diff_snapshots(snap(3, 1.0, player("p1", 0.0)), snap(3, 2.0, player("p1", 6.0))))
    json.loads(dumps(feed))


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Puka Nacua", "P. Nacua"),
        ("Michael Penix Jr.", "M. Penix Jr."),
        ("Amon-Ra St. Brown", "A. St. Brown"),
        ("D.J. Reed", "D.J. Reed"),
        ("James Cook III", "J. Cook III"),
        ("Cher", "Cher"),
        ("", ""),
    ],
)
def test_abbreviate_name(raw: str, expected: str) -> None:
    assert abbreviate_name(raw) == expected


# ---------------------------------------------------------------------------
# Stat deltas — what Yahoo itself prints under each matchup
# ---------------------------------------------------------------------------


def test_an_event_carries_what_changed_not_only_how_much():
    before = snap(1, 100.0, player("p1", 13.67, name="Drake Maye",
                                   stats={"completions": 11.0, "passingYards": 71.0}))
    after = snap(1, 145.0, player("p1", 13.96, name="Drake Maye",
                                  stats={"completions": 12.0, "passingYards": 84.0}))

    (event,) = diff_snapshots(before, after)
    assert event.stat_delta == "1 Comp, 13 Pass Yds"
    assert describe(event) == "D. Maye 1 Comp, 13 Pass Yds"


def test_a_bare_point_delta_is_the_fallback_without_stats():
    """The HTML tier carries no stat lines; the banner still has to say something."""
    before = snap(1, 100.0, player("p1", 10.0))
    after = snap(1, 145.0, player("p1", 16.4))

    (event,) = diff_snapshots(before, after)
    assert event.stat_delta == ""
    assert describe(event) == "P. Nacua +6.40"


def test_a_real_play_description_still_wins():
    """ESPN enrichment says more than either, so it stays the top tier."""
    before = snap(1, 100.0, player("p1", 10.0, stats={"receptions": 2.0}))
    after = snap(1, 145.0, player("p1", 16.4, stats={"receptions": 3.0}))

    (event,) = diff_snapshots(before, after)
    enriched = replace(event, plays=(MatchedPlay("Nacua 24 Yd pass from Stafford", "receiver", 1.0),))
    assert describe(enriched) == "Nacua 24 Yd pass from Stafford"


def test_a_correction_is_never_dressed_up_as_a_play():
    before = snap(1, 100.0, player("p1", 5.4, stats={"rushingYards": 24.0}))
    after = snap(1, 145.0, player("p1", 5.2, stats={"rushingYards": 22.0}))

    (event,) = diff_snapshots(before, after)
    assert event.correction
    assert describe(event) == "P. Nacua -0.20 (stat correction)"


def test_stat_delta_survives_a_restart():
    """History is persisted through Store; the text must come back with it."""
    before = snap(1, 100.0, player("p1", 13.67, stats={"completions": 11.0}))
    after = snap(1, 145.0, player("p1", 13.96, stats={"completions": 12.0}))

    feed = PlayFeed()
    feed.add(diff_snapshots(before, after))
    restored = loads(dumps(feed))

    assert restored.last_play().stat_delta == "1 Comp"


def test_history_written_before_stat_deltas_existed_still_loads():
    legacy = (
        '{"week": 1, "events": [{"event_id": "w1:p1:0.00->6.40", "week": 1,'
        ' "timestamp": 1.0, "player_key": "p1", "player_name": "Puka Nacua",'
        ' "team_key": "t1", "matchup_id": "m1", "nfl_team": "LAR", "position": "WR",'
        ' "delta": 6.4, "old_points": 0.0, "new_points": 6.4, "starter": true,'
        ' "correction": false, "plays": []}]}'
    )
    restored = loads(legacy)
    assert restored.last_play().stat_delta == ""


# ---------------------------------------------------------------------------
# Yahoo's own play descriptions
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _relay():
    from yahoo_fantasy_football.yahoo_redzone import parse_relay_players, parse_relay_plays

    return (
        parse_relay_plays((FIXTURES_DIR / "yahoo_relay_plays_26_2026_w1.txt").read_text()),
        parse_relay_players((FIXTURES_DIR / "yahoo_relay_players_2026_w1.txt").read_text()),
    )


def _scoring_event(player_id: str, **kw):
    from yahoo_fantasy_football.plays import ScoringEvent

    return ScoringEvent(
        event_id=f"e:{player_id}",
        week=1,
        timestamp=100.0,
        player_key=f"802904.p.{player_id}",
        player_name="Whoever",
        team_key="802904.t.1",
        matchup_id="w1.m1",
        nfl_team="Sea",
        position="WR",
        delta=1.3,
        old_points=0.0,
        new_points=1.3,
        starter=True,
        correction=False,
        **kw,
    )


def test_a_play_is_matched_by_id_not_by_name() -> None:
    """The feed writes people as [40041]; so does the event's player key."""
    plays, names = _relay()
    match = match_relay_play(_scoring_event("40041"), plays, names)

    assert match is not None
    assert "Jaxon Smith-Njigba" in match.text
    assert match.confidence == 1.0


def test_the_newest_qualifying_play_wins() -> None:
    """Points cannot be split across plays, so the latest is the best guess."""
    plays, names = _relay()
    mine = [p for p in plays if "40041" in p.player_ids]
    assert len(mine) > 1, "fixture must exercise the choice"

    match = match_relay_play(_scoring_event("40041"), plays, names)
    assert match.play_id == f"{mine[-1].game_key}.{mine[-1].sequence}"


def test_the_short_form_is_player_first_and_drops_the_repeated_category() -> None:
    """``1 Rec, 1 Rec Yds`` reads ``1 rec, 1 yd`` once the player is the subject."""
    from yahoo_fantasy_football.plays import short_describe

    event = replace(
        _scoring_event("33413", stat_delta="1 Rec, 1 Rec Yds"),
        player_name="Travis Etienne Jr.",
    )
    assert short_describe(event) == "T. Etienne Jr. 1 rec, 1 yd"


def test_the_short_form_splits_between_the_player_and_the_result() -> None:
    """The card breaks the line between the halves when a row is too narrow,
    so the halves have to be exactly what the joined form is made of."""
    from yahoo_fantasy_football.plays import short_describe, short_parts

    event = replace(
        _scoring_event("33413", stat_delta="1 Rec, 23 Rec Yds, 1 Rec TD"),
        player_name="Amon-Ra St. Brown",
    )
    assert short_parts(event) == ("A. St. Brown", "1 rec, 23 yds, 1 TD")
    assert " ".join(short_parts(event)) == short_describe(event)

    # Nothing to say about the stat line: the delta is the result.
    bare = replace(_scoring_event("33413", stat_delta=""), player_name="Justin Herbert")
    assert short_parts(bare) == ("J. Herbert", f"{bare.delta:+.2f}")


def test_the_short_form_keeps_a_category_nothing_else_established() -> None:
    """Alone, the yards must still say what they were for."""
    from yahoo_fantasy_football.yahoo_redzone import shorten_stat_delta

    assert shorten_stat_delta("3 Rush Yds") == "3 rush yds"
    assert shorten_stat_delta("1 Rush, 3 Rush Yds") == "1 rush, 3 yds"


def test_the_short_form_leaves_already_short_labels_alone() -> None:
    from yahoo_fantasy_football.yahoo_redzone import shorten_stat_delta

    assert shorten_stat_delta("1 FG") == "1 FG"
    assert shorten_stat_delta("1 PAT") == "1 PAT"
    assert shorten_stat_delta("1 Rec, 12 Rec Yds, 1 Rec TD") == "1 rec, 12 yds, 1 TD"


def test_the_short_form_ignores_the_play_description() -> None:
    """The long sentence is the expanded list's job, not the row's."""
    from yahoo_fantasy_football.plays import describe, short_describe

    plays, names = _relay()
    event = _scoring_event("40041", stat_delta="1 Rec, 13 Rec Yds")
    described = replace(event, plays=(match_relay_play(event, plays, names),))

    assert "passed to" in describe(described)          # long form keeps the sentence
    assert "passed to" not in short_describe(described)
    assert short_describe(described).endswith("1 rec, 13 yds")


def test_a_pinned_revision_keeps_its_own_play_instead_of_the_newest() -> None:
    """The 2026-09-13 live bug: revision must re-read, not re-pick.

    An event matched to an EARLIER play must keep that play when it is read
    again, even though a newer one by the same player now exists. Without the
    pin the newest-wins rule relabelled a 10-yard rush with the touchdown that
    came two minutes later, and two events rendered one identical sentence.
    """
    plays, names = _relay()
    mine = [p for p in plays if "40041" in p.player_ids]
    assert len(mine) > 1, "fixture must exercise the choice"

    first = mine[0]
    pinned = f"{first.game_key}.{first.sequence}"
    event = _scoring_event("40041")

    # Unpinned, this same call returns the NEWEST play — that is the contract
    # for a new event, and what made the pin necessary for an old one.
    assert match_relay_play(event, plays, names).play_id != pinned

    match = match_relay_play(event, plays, names, pin=pinned)
    assert match is not None
    assert match.play_id == pinned


def test_a_pin_that_no_longer_exists_keeps_the_existing_text() -> None:
    """A play ageing out of the feed must not relabel the event.

    Returning None leaves the caller's text alone, which is the safe half of
    the trade: stale-but-right beats fresh-but-wrong.
    """
    plays, names = _relay()
    assert match_relay_play(_scoring_event("40041"), plays, names, pin="nope.999") is None


def test_a_player_who_did_nothing_gets_no_description() -> None:
    plays, names = _relay()
    assert match_relay_play(_scoring_event("99999999"), plays, names) is None


def test_a_fresh_event_does_not_take_a_play_that_predates_it() -> None:
    """The stat feed runs 15-20 s ahead of the play text (measured 2026-09-17).

    So when the event is raised, the play it came from is usually not in the
    feed yet — and the newest play mentioning the player is their PREVIOUS
    one. Matching that and pinning it captioned a 4-yard catch with the
    touchdown from the quarter before. The floor is where the feed stood at
    the end of the previous poll: nothing at or below it can be this event.
    """
    plays, names = _relay()
    mine = [p for p in plays if "40041" in p.player_ids]
    assert len(mine) > 1, "fixture must exercise the choice"
    newest = mine[-1]

    # The feed as it stood before the play landed: everything up to the newest
    # play the player has. The event was raised by a play not yet in it.
    before = [p for p in plays if p.sequence < newest.sequence]
    event = _scoring_event("40041", play_floor=before[-1].sequence)
    assert match_relay_play(event, before, names) is None, "the previous play is not it"

    # Next poll: the text has landed, and it is the only candidate.
    match = match_relay_play(event, plays, names)
    assert match is not None
    assert match.play_id == f"{newest.game_key}.{newest.sequence}"


def test_no_floor_falls_back_to_newest_wins() -> None:
    """Events restored from before the floor existed, or raised with no feed state known."""
    plays, names = _relay()
    mine = [p for p in plays if "40041" in p.player_ids]
    match = match_relay_play(_scoring_event("40041", play_floor=None), plays, names)
    assert match.play_id == f"{mine[-1].game_key}.{mine[-1].sequence}"


def test_a_pinned_event_ignores_the_floor() -> None:
    """A revision re-renders the play it already matched, wherever that sits."""
    plays, names = _relay()
    first = next(p for p in plays if "40041" in p.player_ids)
    pinned = f"{first.game_key}.{first.sequence}"
    event = _scoring_event("40041", play_floor=first.sequence + 500)
    assert match_relay_play(event, plays, names, pin=pinned).play_id == pinned


def test_the_floor_survives_a_restart() -> None:
    feed = PlayFeed()
    feed.add([_scoring_event("40041", play_floor=57)])
    assert loads(dumps(feed)).last_play().play_floor == 57


def test_the_description_wins_over_the_stat_line() -> None:
    """A real sentence beats "1 Rec, 13 Rec Yds" — that is the whole point."""
    from yahoo_fantasy_football.plays import describe

    plays, names = _relay()
    event = _scoring_event("40041", stat_delta="1 Rec, 13 Rec Yds")
    assert describe(event).endswith("1 Rec, 13 Rec Yds")

    described = replace(event, plays=(match_relay_play(event, plays, names),))
    assert "passed to Jaxon Smith-Njigba" in describe(described)


def test_a_revision_replaces_the_text_in_place() -> None:
    """Yahoo posts terse first, fuller a moment later."""
    from yahoo_fantasy_football.plays import MatchedPlay, PlayFeed

    feed = PlayFeed()
    event = _scoring_event("40041")
    feed.add([event])
    terse = MatchedPlay(text="Sam Darnold complete for 13 yards", role="", confidence=1.0)
    feed.revise(event.event_id, (terse,))
    assert feed.last_play().plays[-1].text == terse.text

    fuller = MatchedPlay(
        text="Sam Darnold passed to Jaxon Smith-Njigba for 13 yard gain", role="", confidence=1.0
    )
    feed.revise(event.event_id, (fuller,))

    assert len(feed) == 1, "a revision must not duplicate the event"
    assert feed.last_play().plays[-1].text == fuller.text


def test_revising_an_event_that_is_not_there_is_harmless() -> None:
    from yahoo_fantasy_football.plays import PlayFeed

    assert PlayFeed().revise("nope", ()) is None


# ---------------------------------------------------------------------------
# One play, in pieces
# ---------------------------------------------------------------------------
#
# Yahoo lands a play's stats a category at a time, a poll apart. Observed
# live 2026-09-21 (Rams-Giants), on every reception of the night: a 19-yard
# catch arrived as "1 Rec" +1.00, then "19 Rec Yds" +1.90, then "1 Rec Yds"
# +0.10, ten seconds apart, and each piece was captioned with the same
# sentence once the text landed. One play is one row.


def _pinned(play_id: str, text: str = "Matthew Stafford passed to Davante Adams") -> MatchedPlay:
    return MatchedPlay(text=text, role="", confidence=1.0, play_id=play_id)


def _piece(event_id: str, at: float, delta: float, stat: str, *, plays=(), **kw):
    from yahoo_fantasy_football.plays import ScoringEvent

    return ScoringEvent(
        event_id=event_id,
        week=2,
        timestamp=at,
        player_key=kw.pop("player_key", "802904.p.27581"),
        player_name=kw.pop("player_name", "Davante Adams"),
        team_key="802904.t.1",
        matchup_id="w2.m1",
        nfl_team="LAR",
        position="WR",
        delta=delta,
        old_points=kw.pop("old_points", 0.0),
        new_points=kw.pop("new_points", delta),
        starter=True,
        correction=kw.pop("correction", False),
        plays=tuple(plays),
        stat_delta=stat,
        **kw,
    )


def test_a_later_piece_of_the_same_play_joins_its_row() -> None:
    feed = PlayFeed()
    first = _piece("e1", 100.0, 1.0, "1 Rec", plays=[_pinned("14.30")], new_points=1.0)
    assert feed.add([first]) == [first]

    second = _piece("e2", 110.0, 1.9, "19 Rec Yds", plays=[_pinned("14.30")], new_points=2.9)
    (row,) = feed.add([second])

    assert len(feed) == 1, "one play, one row"
    assert row.event_id == "e1", "the row keeps the id the bus already carried"
    assert row.delta == 2.9
    assert row.new_points == 2.9
    assert row.stat_delta == "1 Rec, 19 Rec Yds"
    assert row.absorbed == ("e2",)
    assert feed.last_play() == row


def test_every_further_piece_keeps_joining() -> None:
    feed = PlayFeed()
    feed.add([_piece("e1", 100.0, 1.0, "1 Rec", plays=[_pinned("14.30")])])
    feed.add([_piece("e2", 110.0, 1.9, "19 Rec Yds", plays=[_pinned("14.30")])])
    feed.add([_piece("e3", 120.0, 0.1, "1 Rec Yds", plays=[_pinned("14.30")])])

    (row,) = feed.recent(5)
    assert row.delta == 3.0
    assert row.stat_delta == "1 Rec, 20 Rec Yds"
    assert row.absorbed == ("e2", "e3")


def test_pieces_of_different_plays_stay_apart() -> None:
    feed = PlayFeed()
    feed.add([_piece("e1", 100.0, 1.0, "1 Rec", plays=[_pinned("14.30")])])
    feed.add([_piece("e2", 400.0, 7.4, "1 Rec, 64 Rec Yds", plays=[_pinned("14.45")])])
    assert len(feed) == 2


def test_the_passers_share_of_the_play_is_not_the_receivers() -> None:
    """One play credits two players; their rows are two rows."""
    feed = PlayFeed()
    feed.add([_piece("e1", 100.0, 1.0, "1 Rec", plays=[_pinned("14.30")])])
    feed.add(
        [
            _piece(
                "e2", 100.0, 0.25, "1 Comp", plays=[_pinned("14.30")],
                player_key="802904.p.8780", player_name="Matthew Stafford",
            )
        ]
    )
    assert len(feed) == 2


def test_a_correction_never_joins_a_play() -> None:
    """A walked-back yard is shown as what it is, not hidden in the play's total."""
    feed = PlayFeed()
    feed.add([_piece("e1", 100.0, 7.4, "1 Rec, 64 Rec Yds", plays=[_pinned("14.45")])])
    feed.add([_piece("e2", 110.0, -0.1, "-1 Rec Yds", plays=[_pinned("14.45")], correction=True)])
    assert len(feed) == 2
    assert feed.recent(1, include_corrections=True)[0].correction


def test_a_piece_matched_by_revision_joins_the_row_it_now_shares() -> None:
    """The text lands after both pieces were raised: the older row wins."""
    feed = PlayFeed()
    feed.add([_piece("e1", 100.0, 1.0, "1 Rec")])
    feed.add([_piece("e2", 110.0, 1.9, "19 Rec Yds")])
    assert len(feed) == 2

    feed.revise("e2", (_pinned("14.30"),))
    assert len(feed) == 2, "nothing else shows that play yet"

    row = feed.revise("e1", (_pinned("14.30"),))
    assert len(feed) == 1
    assert row.event_id == "e1" and row.delta == 2.9 and row.absorbed == ("e2",)


def test_a_revision_of_the_younger_piece_folds_it_into_the_older() -> None:
    feed = PlayFeed()
    feed.add([_piece("e1", 100.0, 1.0, "1 Rec", plays=[_pinned("14.30")])])
    feed.add([_piece("e2", 110.0, 1.9, "19 Rec Yds")])

    row = feed.revise("e2", (_pinned("14.30"),))
    assert len(feed) == 1
    assert row.event_id == "e1" and row.delta == 2.9


def test_a_folded_piece_stays_folded_across_a_restart() -> None:
    """Re-ingesting the piece's transition after a restart must not raise it again."""
    feed = PlayFeed()
    feed.add([_piece("e1", 100.0, 1.0, "1 Rec", plays=[_pinned("14.30")])])
    feed.add([_piece("e2", 110.0, 1.9, "19 Rec Yds", plays=[_pinned("14.30")])])

    restored = loads(dumps(feed))
    assert restored.last_play().absorbed == ("e2",)
    assert restored.add([_piece("e2", 110.0, 1.9, "19 Rec Yds", plays=[_pinned("14.30")])]) == []
    assert len(restored) == 1 and restored.last_play().delta == 2.9


def test_a_piece_with_no_play_of_its_own_folds_into_the_last_one() -> None:
    """The floor kept it off the play it belongs to; fold() puts it there."""
    feed = PlayFeed()
    feed.add([_piece("e1", 100.0, 0.2, "1 Rush, 2 Rush Yds", plays=[_pinned("14.58")])])
    feed.add([_piece("e2", 200.0, 0.1, "1 Rush Yds", play_floor=59)])

    row = feed.fold("e2")
    assert row is not None and row.event_id == "e1"
    assert len(feed) == 1
    assert row.delta == 0.3
    assert row.stat_delta == "1 Rush, 3 Rush Yds"


def test_a_piece_folds_past_a_correction_in_between() -> None:
    feed = PlayFeed()
    feed.add([_piece("e1", 100.0, 1.6, "1 Rec, 6 Rec Yds", plays=[_pinned("14.53")])])
    feed.add([_piece("e2", 110.0, -0.1, "-1 Rec Yds", correction=True)])
    feed.add([_piece("e3", 120.0, 0.1, "1 Rec Yds")])

    assert feed.fold("e3").delta == 1.7
    assert [e.event_id for e in feed.recent(5, include_corrections=True)] == ["e2", "e1"]


def test_a_piece_stays_when_the_last_play_is_not_known() -> None:
    """An unpinned last row says nothing about where this piece belongs."""
    feed = PlayFeed()
    feed.add([_piece("e1", 100.0, 2.0, "1 Int", player_key="802904.p.100014", player_name="Rams")])
    feed.add([_piece("e2", 110.0, 1.1, "11 ST Ret Yds", player_key="802904.p.100014", player_name="Rams")])
    assert feed.fold("e2") is None
    assert len(feed) == 2


def test_a_piece_stays_when_it_is_too_long_after_the_play() -> None:
    from yahoo_fantasy_football.plays import SPLIT_TICK_SECONDS

    feed = PlayFeed()
    feed.add([_piece("e1", 100.0, 0.2, "1 Rush, 2 Rush Yds", plays=[_pinned("14.58")])])
    feed.add([_piece("e2", 100.0 + SPLIT_TICK_SECONDS + 1, 0.1, "1 Rush Yds")])
    assert feed.fold("e2") is None
    assert len(feed) == 2


def test_fold_leaves_a_matched_event_and_a_correction_alone() -> None:
    feed = PlayFeed()
    feed.add([_piece("e1", 100.0, 0.2, "1 Rush, 2 Rush Yds", plays=[_pinned("14.58")])])
    feed.add([_piece("e2", 110.0, 0.5, "5 Rush Yds", plays=[_pinned("14.60")])])
    feed.add([_piece("e3", 120.0, -0.1, "-1 Rush Yds", correction=True)])
    assert feed.fold("e2") is None
    assert feed.fold("e3") is None
    assert feed.fold("nope") is None
    assert len(feed) == 3


def test_play_above_floor_says_whether_a_piece_has_a_play_to_wait_for() -> None:
    from yahoo_fantasy_football.plays import play_above_floor

    plays, _ = _relay()
    mine = [p for p in plays if "40041" in p.player_ids]
    newest = mine[-1]

    assert play_above_floor(_scoring_event("40041", play_floor=newest.sequence - 1), plays)
    assert not play_above_floor(_scoring_event("40041", play_floor=newest.sequence), plays)
    assert play_above_floor(_scoring_event("40041", play_floor=None), plays), "unknown: cannot rule it out"
    assert not play_above_floor(_scoring_event("99999999", play_floor=0), plays)


def test_the_feed_counts_every_change() -> None:
    """The coordinator persists on a changed count — a revision alone used to be lost."""
    feed = PlayFeed()
    assert feed.version == 0
    feed.add([_piece("e1", 100.0, 1.0, "1 Rec")])
    after_add = feed.version
    assert after_add > 0
    feed.add([_piece("e1", 100.0, 1.0, "1 Rec")])
    assert feed.version == after_add, "a duplicate changes nothing"
    feed.revise("e1", (_pinned("14.30"),))
    assert feed.version > after_add
    after_revise = feed.version
    feed.revise("nope", ())
    assert feed.version == after_revise
    feed.add([_piece("e2", 110.0, 1.9, "19 Rec Yds", plays=[_pinned("14.30")])])
    assert feed.version > after_revise, "a fold is a change"
