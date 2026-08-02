"""Tests for the public-web fetch layer.

The network call is injected, so every path here runs against a fake fetcher —
no aiohttp, no live Yahoo, and no flakiness. What is worth pinning is the
*failure* behaviour, because that is what decides whether a card shows stale
scores, empty scores, or an actionable error.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from yahoo_fantasy_football.web_client import (
    STALE_AFTER_SECONDS,
    FetchFailed,
    LeagueIsPrivate,
    YahooWebClient,
)

FIXTURES = Path(__file__).parent / "fixtures"
MATCHUP = (FIXTURES / "yahoo_web_matchup_2025_w13.html").read_text(encoding="utf-8")
SCOREBOARD = (FIXTURES / "yahoo_web_scoreboard_2025_w13.html").read_text(encoding="utf-8")

LOGIN_URL = "https://login.yahoo.com/?.src=Fantasy&.done=x"


def run(coro):
    return asyncio.run(coro)


class FakeFetcher:
    """Serves the real fixtures, and can be told to fail on demand."""

    def __init__(self, *, fail_matchups: bool = False, fail_all: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_matchups = fail_matchups
        self.fail_all = fail_all

    async def __call__(self, url: str) -> tuple[str, str]:
        self.calls.append(url)
        if self.fail_all:
            raise TimeoutError("boom")
        if "/matchup?" in url:
            if self.fail_matchups:
                raise TimeoutError("boom")
            return MATCHUP, url
        return SCOREBOARD, url

    @property
    def matchup_calls(self) -> list[str]:
        return [u for u in self.calls if "/matchup?" in u]


def client(fetcher, **kw) -> YahooWebClient:
    return YahooWebClient(fetcher, league_id=476807, season=2025, **kw)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_a_refresh_reads_the_board_and_every_matchup() -> None:
    f = FakeFetcher()
    data = run(client(f).async_refresh(now=100.0))

    assert data.week == 13
    assert len(data.standings) == 5
    assert len(data.matchups) == 5
    assert data.team_count == 10
    assert data.fetched_at == 100.0
    assert not data.partial
    assert len(f.matchup_calls) == 5


def test_the_week_is_discovered_from_the_page_not_a_calendar() -> None:
    """No hardcoded season calendar to drift out of date."""
    f = FakeFetcher()
    assert run(client(f).async_get_week()) == 13
    assert "?week=" not in f.calls[0], "week discovery must request the default page"


def test_an_explicit_week_skips_discovery() -> None:
    f = FakeFetcher()
    assert run(client(f).async_get_week(9)) == 9
    assert f.calls == []


def test_rosters_survive_the_fetch_layer() -> None:
    data = run(client(FakeFetcher()).async_refresh(now=0.0))
    assert len(data.matchups[0].players) == 28
    assert data.matchups[0].teams[0].name == "Tesla"


# ---------------------------------------------------------------------------
# A private league is permanent, not transient
# ---------------------------------------------------------------------------


def test_a_private_league_raises_a_distinct_error() -> None:
    """Retrying a private league can never succeed, so it must not look transient."""

    async def to_login(url: str) -> tuple[str, str]:
        return "<html>sign in</html>", LOGIN_URL

    with pytest.raises(LeagueIsPrivate):
        run(client(to_login).async_refresh(now=0.0))


def test_a_private_league_is_not_masked_by_stale_data() -> None:
    """The user has to be told; a frozen card with no explanation is worse."""
    f = FakeFetcher()
    c = client(f)
    run(c.async_refresh(now=0.0))  # prime the cache

    async def to_login(url: str) -> tuple[str, str]:
        return "<html>sign in</html>", LOGIN_URL

    c._fetch = to_login
    with pytest.raises(LeagueIsPrivate):
        run(c.async_refresh_or_stale(now=10.0))


def test_a_login_bounce_is_detected_in_the_body_too() -> None:
    async def body_only(url: str) -> tuple[str, str]:
        return '<html><meta url="https://login.yahoo.com/x">' + "y" * 900, url

    with pytest.raises(LeagueIsPrivate):
        run(client(body_only).async_refresh(now=0.0))


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


def test_one_bad_matchup_does_not_lose_the_others() -> None:
    """The scoreboard still carries every score; only that roster is missing."""

    class OneBad(FakeFetcher):
        async def __call__(self, url: str) -> tuple[str, str]:
            if "mid1=3" in url:
                self.calls.append(url)
                raise TimeoutError("boom")
            return await super().__call__(url)

    data = run(client(OneBad()).async_refresh(now=0.0))
    assert len(data.matchups) == 4
    assert len(data.standings) == 5, "scores are complete even though a roster is not"
    assert data.partial


def test_a_total_failure_falls_back_to_the_last_good_payload() -> None:
    f = FakeFetcher()
    c = client(f)
    fresh = run(c.async_refresh(now=0.0))

    f.fail_all = True
    served = run(c.async_refresh_or_stale(now=60.0))
    assert served is fresh, "a card showing 60s-old scores beats an empty one"


def test_stale_data_expires_rather_than_pretending_to_be_live() -> None:
    f = FakeFetcher()
    c = client(f)
    run(c.async_refresh(now=0.0))

    f.fail_all = True
    with pytest.raises(FetchFailed):
        run(c.async_refresh_or_stale(now=STALE_AFTER_SECONDS + 1))


def test_a_first_ever_failure_has_nothing_to_serve_and_says_so() -> None:
    with pytest.raises(FetchFailed):
        run(client(FakeFetcher(fail_all=True)).async_refresh_or_stale(now=0.0))


def test_a_truncated_response_is_treated_as_a_failure() -> None:
    """Yahoo serves 200 on error pages, so status codes cannot be trusted."""

    async def stub(url: str) -> tuple[str, str]:
        return "<html>oops</html>", url

    with pytest.raises(FetchFailed):
        run(client(stub).async_refresh(now=0.0))


def test_an_empty_scoreboard_is_a_failure_not_an_empty_league() -> None:
    async def no_matchups(url: str) -> tuple[str, str]:
        return "<html>" + "x" * 900 + "Week 13 Matchups</html>", url

    with pytest.raises(FetchFailed):
        run(client(no_matchups).async_refresh(now=0.0))


# ---------------------------------------------------------------------------
# Politeness — this is a scraper, and must stay bounded
# ---------------------------------------------------------------------------


def test_matchup_fetches_are_hard_capped() -> None:
    """A page reporting nonsense must not turn into unbounded requests."""
    f = FakeFetcher()
    data = run(client(f, max_matchups=2).async_refresh(now=0.0))
    assert len(f.matchup_calls) == 2
    assert data.partial, "a capped refresh is incomplete and must say so"


def test_a_refresh_costs_one_request_per_matchup_plus_one() -> None:
    f = FakeFetcher()
    run(client(f).async_refresh(now=0.0, week=13))
    assert len(f.calls) == 6, "1 scoreboard + 5 matchups; no redundant fetches"
