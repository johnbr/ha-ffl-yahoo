"""Fetch orchestration and degradation for the GameChannel tier.

The network call is injected, so every path here — including the ones that only
happen when Yahoo misbehaves — is exercised without a live Yahoo.
"""

from __future__ import annotations

import pytest
from conftest import FIXTURES
from yahoo_fantasy_football.redzone_client import (
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
