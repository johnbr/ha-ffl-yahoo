"""Fetch orchestration and degradation for the GameChannel tier.

The network call is injected, so every path here — including the ones that only
happen when Yahoo misbehaves — is exercised without a live Yahoo.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import FIXTURES
from yahoo_fantasy_football.redzone_client import (
    FEED_HOLD_SECONDS,
    PLAYERS_RETRY_SECONDS,
    PLAYERS_TTL_SECONDS,
    PLAYS_MIN_INTERVAL_SECONDS,
    PLAYS_RECHECK_SECONDS,
    SEED_TTL_SECONDS,
    FetchFailed,
    RedzoneClient,
)
from yahoo_fantasy_football.web_client import STALE_AFTER_SECONDS

LEAGUE = "999999"


def _bodies() -> dict[str, str]:
    return {
        "redzone": (FIXTURES / "yahoo_redzone_2026_w1.json").read_text(),
        "stats": (FIXTURES / "yahoo_relay_stats_2026_w1.txt").read_text(),
        "games": (FIXTURES / "yahoo_relay_games_2026_w1.txt").read_text(),
    }


def _fetcher(bodies: dict[str, str], fail: set[str] | None = None, log: list | None = None):
    fail = fail or set()

    async def fetch(url: str) -> str:
        if log is not None:
            log.append(url)
        for key, body in bodies.items():
            if key in url:
                if key in fail:
                    raise OSError(f"boom on {key}")
                return body
        raise OSError(f"unexpected url {url}")

    return fetch


def _scores(data) -> dict[str, float]:
    return {t.name: t.points for pair in data.standings for t in pair}


@pytest.mark.asyncio
async def test_one_refresh_is_three_requests_whatever_the_league_size():
    log: list[str] = []
    client = RedzoneClient(_fetcher(_bodies(), log=log), LEAGUE)
    data = await client.async_refresh(now=100.0)

    assert len(log) == 3, "no per-matchup fan-out — that was the HTML tier's cost"
    assert len(data.standings) == 5
    assert data.fetched_at == 100.0


@pytest.mark.asyncio
async def test_a_missing_live_feed_is_not_a_failure():
    """Before the first kickoff Yahoo serves these empty. Zeroes are correct."""
    client = RedzoneClient(_fetcher(_bodies(), fail={"stats", "games"}), LEAGUE)
    data = await client.async_refresh(now=0.0)

    assert len(data.standings) == 5
    assert all(t.points == 0.0 for pair in data.standings for t in pair)


@pytest.mark.asyncio
async def test_a_dropped_stat_feed_does_not_score_the_league_zero():
    """The bug of 2026-09-24: one failed fetch read as everybody losing their game.

    Points are computed from the stat feed, so an empty body scores every
    player zero — and the poll after it re-emits their whole game as a single
    scoring play. Holding the last body over the gap keeps the points still,
    which is what a poll with no news should look like.
    """
    bodies = _bodies()
    client = RedzoneClient(_fetcher(bodies), LEAGUE)
    good = await client.async_refresh(now=100.0)
    scored = _scores(good)
    assert any(scored.values()), "fixture must have live points for this to mean anything"

    client._fetch = _fetcher(bodies, fail={"stats"})
    held = await client.async_refresh(now=130.0)

    assert _scores(held) == scored
    assert held.fetched_at == 130.0, "the rest of the poll is fresh — only the stats are held"


@pytest.mark.asyncio
async def test_a_held_stat_feed_stops_pretending_eventually():
    """Frozen points under a running clock are their own kind of wrong."""
    bodies = _bodies()
    client = RedzoneClient(_fetcher(bodies), LEAGUE)
    await client.async_refresh(now=100.0)

    client._fetch = _fetcher(bodies, fail={"stats"})
    with pytest.raises(FetchFailed, match="no stats feed"):
        await client.async_refresh(now=100.0 + STALE_AFTER_SECONDS + 1)


@pytest.mark.asyncio
async def test_a_recovered_stat_feed_is_preferred_to_the_copy_in_hand():
    bodies = _bodies()
    client = RedzoneClient(_fetcher(bodies), LEAGUE)
    await client.async_refresh(now=100.0)

    client._fetch = _fetcher(bodies, fail={"stats"})
    await client.async_refresh(now=130.0)

    # Back up, and with a body that moves a starter: Drake Maye's 36 rushing
    # yards become 136, which is ten points on his team's total.
    moved = bodies["stats"].replace("r|40881|5|36|0|16|0|0|3", "r|40881|5|136|0|16|0|0|3", 1)
    assert moved != bodies["stats"], "the mutation must land or this proves nothing"
    bodies["stats"] = moved
    client._fetch = _fetcher(bodies)
    recovered = await client.async_refresh(now=160.0)

    assert _scores(recovered)["Blitz Brigade"] == pytest.approx(22.74)

    # And the hold-over window restarts from the fetch that succeeded, so the
    # copy it holds from here is the new one, not the one from t=100.
    client._fetch = _fetcher(bodies, fail={"stats"})
    later = await client.async_refresh(now=160.0 + STALE_AFTER_SECONDS - 1)
    assert _scores(later) == _scores(recovered)


@pytest.mark.asyncio
async def test_a_dropped_games_feed_does_not_erase_the_slate():
    """The bug of 2026-09-25: one failed fetch read as "no games exist".

    An empty games body is not "nothing is live", it is "no idea" — the NFL
    card's whole slate vanishes, every matchup reads final because no starter
    has a game left, and the cadence drops to the near-game interval, so the
    blank outlives the request that caused it by five minutes.
    """
    bodies = _bodies()
    client = RedzoneClient(_fetcher(bodies), LEAGUE)
    good = await client.async_refresh(now=100.0)
    assert good.active_games == 1, "fixture must have a live game for this to mean anything"

    client._fetch = _fetcher(bodies, fail={"games"})
    held = await client.async_refresh(now=130.0)

    assert len(held.nfl_games) == len(good.nfl_games)
    assert held.active_games == good.active_games
    assert held.live_tick == good.live_tick
    assert held.plays_feeds == good.plays_feeds, "enrichment needs these to survive the gap"


@pytest.mark.asyncio
async def test_the_games_feed_is_held_on_a_shorter_leash_than_the_stats_feed():
    """A stopped clock beside a score gives itself away faster than stale points."""
    assert FEED_HOLD_SECONDS["games"] < FEED_HOLD_SECONDS["stats"]

    bodies = _bodies()
    client = RedzoneClient(_fetcher(bodies), LEAGUE)
    await client.async_refresh(now=100.0)
    client._fetch = _fetcher(bodies, fail={"games"})

    # Inside the window the slate stands; past it the refresh fails, which
    # hands the poll to async_refresh_or_stale rather than serving a hole.
    inside = await client.async_refresh(now=100.0 + FEED_HOLD_SECONDS["games"] - 1)
    assert inside.active_games == 1

    with pytest.raises(FetchFailed, match="no games feed"):
        await client.async_refresh(now=100.0 + FEED_HOLD_SECONDS["games"] + 1)


@pytest.mark.asyncio
async def test_a_feed_that_never_arrived_is_not_held():
    """Between seasons there is nothing to hold, and zeroes are the truth."""
    client = RedzoneClient(_fetcher(_bodies(), fail={"stats", "games"}), LEAGUE)
    data = await client.async_refresh(now=0.0)

    assert data.nfl_games == [] and data.active_games == 0
    assert all(t.points == 0.0 for pair in data.standings for t in pair)


@pytest.mark.asyncio
async def test_one_feed_dropping_does_not_hold_the_other():
    """The two are held independently — a stats blip must not freeze the clock."""
    bodies = _bodies()
    client = RedzoneClient(_fetcher(bodies), LEAGUE)
    await client.async_refresh(now=100.0)

    moved = bodies["games"].replace("|14:52|", "|11:07|", 1)
    assert moved != bodies["games"], "the mutation must land or this proves nothing"
    bodies["games"] = moved

    client._fetch = _fetcher(bodies, fail={"stats"})
    served = await client.async_refresh(now=130.0)

    assert "11:07" in served.live_tick, "the games feed arrived and must be used"


@pytest.mark.asyncio
async def test_a_missing_seed_is_a_failure():
    """Without the league itself there is nothing to render at all."""
    client = RedzoneClient(_fetcher(_bodies(), fail={"redzone"}), LEAGUE)
    with pytest.raises(FetchFailed):
        await client.async_refresh(now=0.0)


@pytest.mark.asyncio
async def test_a_truncated_response_is_rejected():
    client = RedzoneClient(_fetcher({"redzone": "{}"}), LEAGUE)
    with pytest.raises(FetchFailed, match="suspiciously short"):
        await client.async_refresh(now=0.0)


@pytest.mark.asyncio
async def test_a_wrong_league_id_says_so():
    client = RedzoneClient(_fetcher(_bodies()), "123456")
    with pytest.raises(FetchFailed, match="could not read league"):
        await client.async_refresh(now=0.0)


@pytest.mark.asyncio
async def test_stale_data_beats_an_empty_card():
    bodies = _bodies()
    client = RedzoneClient(_fetcher(bodies), LEAGUE)
    good = await client.async_refresh(now=100.0)

    # Past the seed TTL, so the failing seed fetch is actually attempted.
    client._fetch = _fetcher(bodies, fail={"redzone"})
    served = await client.async_refresh_or_stale(now=100.0 + SEED_TTL_SECONDS + 1)

    assert served is good
    assert served.fetched_at == 100.0, "stale data keeps its original timestamp"


@pytest.mark.asyncio
async def test_the_seed_is_not_refetched_on_every_poll():
    """The 178 KB payload is what made a fast live cadence expensive."""
    log: list[str] = []
    client = RedzoneClient(_fetcher(_bodies(), log=log), LEAGUE)

    await client.async_refresh(now=100.0)
    assert sum("redzone" in u for u in log) == 1

    log.clear()
    await client.async_refresh(now=100.0 + SEED_TTL_SECONDS - 1)
    assert log == [u for u in log if "redzone" not in u], "seed refetched inside its TTL"
    assert len(log) == 2, "the two live feeds are still fetched every time"

    log.clear()
    await client.async_refresh(now=100.0 + SEED_TTL_SECONDS + 1)
    assert sum("redzone" in u for u in log) == 1, "seed must refresh once it ages out"


@pytest.mark.asyncio
async def test_a_seed_that_cannot_be_read_is_not_cached():
    """Otherwise every poll fails identically until the TTL runs out."""
    bodies = _bodies()
    bodies["redzone"] = '{"service": {"leagues": {}}}'
    client = RedzoneClient(_fetcher(bodies), LEAGUE)

    with pytest.raises(FetchFailed):
        await client.async_refresh(now=100.0)
    assert client._seed is None


@pytest.mark.asyncio
async def test_stale_data_stops_pretending_eventually():
    bodies = _bodies()
    client = RedzoneClient(_fetcher(bodies), LEAGUE)
    await client.async_refresh(now=100.0)

    client._fetch = _fetcher(bodies, fail={"redzone"})
    with pytest.raises(FetchFailed, match="no fresh data"):
        await client.async_refresh_or_stale(now=100.0 + STALE_AFTER_SECONDS + 1)


@pytest.mark.asyncio
async def test_first_ever_refresh_has_nothing_to_fall_back_on():
    client = RedzoneClient(_fetcher(_bodies(), fail={"redzone"}), LEAGUE)
    with pytest.raises(FetchFailed):
        await client.async_refresh_or_stale(now=0.0)


@pytest.mark.asyncio
async def test_team_choices_for_the_config_flow():
    client = RedzoneClient(_fetcher(_bodies()), LEAGUE)
    teams = await client.async_team_choices()

    assert len(teams) == 10
    assert all(isinstance(name, str) and name for name in teams.values())
    assert await client.async_league_name() == "Test League"


# -- the play feed: conditional, cached, asked for on every poll ----------------

PLAYS_V1 = "# nfl/plays-2.txt\np|2|1|1|10|65|8|1|15:00|2|9|[1] passed to [2] for 9 yard gain\n"
PLAYS_V2 = PLAYS_V1 + "p|2|2|2|1|56|8|1|14:30|1|1|[3] rushed for a 1 yard touchdown\n"


class _Relay:
    """A play feed that honours ``If-Modified-Since`` the way Yahoo's does."""

    def __init__(self, body: str, modified: str = "Thu, 17 Sep 2026 02:14:19 GMT") -> None:
        self.body, self.modified = body, modified
        self.asks: list[str | None] = []
        self.fail = False

    def publish(self, body: str, modified: str) -> None:
        self.body, self.modified = body, modified

    async def fetch_if_modified(self, url: str, since: str | None) -> tuple[str | None, str]:
        self.asks.append(since)
        if self.fail:
            raise OSError("boom")
        if since is not None and since == self.modified:
            return None, since
        return self.body, self.modified


def _plays_client(relay: _Relay) -> RedzoneClient:
    return RedzoneClient(_fetcher(_bodies()), LEAGUE, fetch_if_modified=relay.fetch_if_modified)


@pytest.mark.asyncio
async def test_an_unchanged_play_feed_is_a_304_and_the_cached_body():
    relay = _Relay(PLAYS_V1)
    client = _plays_client(relay)

    assert await client.async_plays("2", now=100.0) == PLAYS_V1
    assert relay.asks == [None], "nothing to condition the first ask on"

    assert await client.async_plays("2", now=110.0) == PLAYS_V1
    assert relay.asks[-1] == relay.modified, "the second ask carries If-Modified-Since"


@pytest.mark.asyncio
async def test_a_changed_play_feed_arrives_on_the_next_poll():
    """The whole point: the text lands after the snap, and the next poll sees it."""
    relay = _Relay(PLAYS_V1)
    client = _plays_client(relay)
    await client.async_plays("2", now=100.0)

    relay.publish(PLAYS_V2, "Thu, 17 Sep 2026 02:14:49 GMT")
    assert await client.async_plays("2", now=110.0) == PLAYS_V2
    assert await client.async_plays("2", now=120.0) == PLAYS_V2
    assert relay.asks[-1] == "Thu, 17 Sep 2026 02:14:49 GMT", "conditioned on the NEW mtime"


@pytest.mark.asyncio
async def test_two_asks_in_one_poll_are_one_request():
    """Fantasy enrichment and the games card both want the same feed each poll."""
    relay = _Relay(PLAYS_V1)
    client = _plays_client(relay)

    await client.async_plays("2", now=100.0)
    await client.async_plays("2", now=100.0)
    await client.async_plays("2", now=100.0 + PLAYS_MIN_INTERVAL_SECONDS - 1)
    assert len(relay.asks) == 1

    await client.async_plays("2", now=100.0 + PLAYS_MIN_INTERVAL_SECONDS)
    assert len(relay.asks) == 2


@pytest.mark.asyncio
async def test_a_304_is_not_trusted_forever():
    """Last-Modified is one-second and per-origin; a full fetch confirms it now and then."""
    relay = _Relay(PLAYS_V1)
    client = _plays_client(relay)

    await client.async_plays("2", now=100.0)
    await client.async_plays("2", now=100.0 + PLAYS_RECHECK_SECONDS / 2)
    assert relay.asks[-1] is not None
    await client.async_plays("2", now=100.0 + PLAYS_RECHECK_SECONDS + 1)
    assert relay.asks[-1] is None, "past the recheck window the ask is unconditional"
    await client.async_plays("2", now=100.0 + PLAYS_RECHECK_SECONDS + 20)
    assert relay.asks[-1] is not None, "and the full fetch restarts the window"


@pytest.mark.asyncio
async def test_a_failed_play_fetch_serves_what_it_had():
    """Stale text beats a blank line for one bad poll; nothing at all beats a raise."""
    relay = _Relay(PLAYS_V1)
    client = _plays_client(relay)
    relay.fail = True
    assert await client.async_plays("2", now=100.0) == ""

    relay.fail = False
    await client.async_plays("2", now=110.0)
    relay.fail = True
    assert await client.async_plays("2", now=120.0) == PLAYS_V1


@pytest.mark.asyncio
async def test_play_feeds_are_per_game():
    relay = _Relay(PLAYS_V1)
    client = _plays_client(relay)
    await client.async_plays("2", now=100.0)
    await client.async_plays("7", now=100.0)
    assert relay.asks == [None, None], "game 7 cannot be conditioned on game 2's mtime"


@pytest.mark.asyncio
async def test_without_a_conditional_fetcher_the_feed_is_fetched_whole():
    """The config flow and the seed-only tests never supply one."""
    log: list[str] = []
    bodies = _bodies() | {"plays-2": PLAYS_V1}
    client = RedzoneClient(_fetcher(bodies, log=log), LEAGUE)
    assert await client.async_plays("2", now=100.0) == PLAYS_V1
    assert await client.async_plays("2", now=100.0) == PLAYS_V1
    assert sum("plays-2" in u for u in log) == 2


# -- the player dictionary: cached by age, refetched when it is missing a name --

PLAYERS_V1 = "# nfl/players.txt\nm|1|8|QB|Jared|Goff|-|16\n"
PLAYERS_V2 = PLAYERS_V1 + "m|2|8|RB|Jahmyr|Gibbs|-|26\n"


class _Dictionary:
    """A players feed that grows as the games are played."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.asks = 0

    async def fetch(self, url: str) -> str:
        if "players" in url:
            self.asks += 1
            await asyncio.sleep(0)  # a real fetch yields; concurrent askers interleave here
            return self.body
        return _bodies()["redzone"]


@pytest.mark.asyncio
async def test_a_name_the_dictionary_lacks_refetches_it():
    """A player's first touch of the day names an id the copy in hand has never
    seen; the play is unrenderable until the dictionary is read again."""
    feed = _Dictionary(PLAYERS_V1)
    client = RedzoneClient(feed.fetch, LEAGUE)

    first = await client.async_players(100.0, needed=("1",))
    assert first == {"1": "Jared Goff"} and feed.asks == 1

    feed.body = PLAYERS_V2
    later = 100.0 + PLAYERS_RETRY_SECONDS
    assert await client.async_players(later, needed=("1", "2")) == {"1": "Jared Goff", "2": "Jahmyr Gibbs"}
    assert feed.asks == 2, "an unknown id is the signal the dictionary has grown"


@pytest.mark.asyncio
async def test_a_refetch_for_a_missing_name_is_floored():
    """An id that never resolves must not turn every poll into a full fetch."""
    feed = _Dictionary(PLAYERS_V1)
    client = RedzoneClient(feed.fetch, LEAGUE)
    await client.async_players(100.0)

    for tick in range(1, 4):
        await client.async_players(100.0 + tick * PLAYERS_RETRY_SECONDS / 4, needed=("99",))
    assert feed.asks == 1, "three polls inside the floor, none of them a fetch"

    await client.async_players(100.0 + PLAYERS_RETRY_SECONDS, needed=("99",))
    assert feed.asks == 2
    await client.async_players(100.0 + PLAYERS_RETRY_SECONDS + 10, needed=("99",))
    assert feed.asks == 2, "and the floor restarts from that fetch"


@pytest.mark.asyncio
async def test_a_dictionary_that_knows_every_name_is_kept_by_age():
    feed = _Dictionary(PLAYERS_V1)
    client = RedzoneClient(feed.fetch, LEAGUE)
    first = await client.async_players(100.0)

    for tick in range(1, 20):
        got = await client.async_players(100.0 + tick * PLAYERS_RETRY_SECONDS, needed=("1",))
        assert got is first, "same object until something replaces it — callers compare by identity"
    assert feed.asks == 1

    await client.async_players(100.0 + PLAYERS_TTL_SECONDS, needed=("1",))
    assert feed.asks == 2, "age alone still refreshes the quiet case"


@pytest.mark.asyncio
async def test_a_failed_dictionary_refetch_keeps_the_copy_in_hand():
    feed = _Dictionary(PLAYERS_V1)
    client = RedzoneClient(feed.fetch, LEAGUE)
    first = await client.async_players(100.0)

    feed.body = ""
    assert await client.async_players(100.0 + PLAYERS_RETRY_SECONDS, needed=("2",)) is first


@pytest.mark.asyncio
async def test_eight_games_asking_at_once_is_one_fetch():
    """The last-play refresh gathers every live game; they share the read."""
    feed = _Dictionary(PLAYERS_V1)
    client = RedzoneClient(feed.fetch, LEAGUE)
    await client.async_players(100.0)

    feed.body = PLAYERS_V2
    later = 100.0 + PLAYERS_RETRY_SECONDS
    got = await asyncio.gather(*(client.async_players(later, needed=("2",)) for _ in range(8)))
    assert feed.asks == 2
    assert all(d is got[0] for d in got), "one dictionary object, handed to all eight"
