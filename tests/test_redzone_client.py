"""Fetch orchestration and degradation for the GameChannel tier.

The network call is injected, so every path here — including the ones that only
happen when Yahoo misbehaves — is exercised without a live Yahoo.
"""

from __future__ import annotations

import pytest
from conftest import FIXTURES
from yahoo_fantasy_football.redzone_client import (
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
