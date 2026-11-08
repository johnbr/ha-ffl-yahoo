"""HTTP contracts: status/body handling, confidentiality and conditional fetches."""

import json

import pytest
from http_fakes import HttpFailure, Response, Session
from yahoo_fantasy_football.http_client import (
    YahooAccessDenied,
    YahooRefused,
    fetch_text,
    league_refused,
    normalize_cookie,
)
from yahoo_fantasy_football.redzone_client import RedzoneClient
from yahoo_fantasy_football.web_client import FetchFailed

URL = "https://pub-api.fantasysports.yahoo.com/fantasy/v3/redzone/nfl?league_id=726304"


@pytest.mark.parametrize("status", [401, 403])
async def test_missing_league_is_distinct_and_handles_chunked_body(status):
    body = json.dumps({"error": {"description": "Unable to retrieve cookie.", "detail": "SECRET"}})
    session = Session(Response(status, body, chunk_size=7))
    with pytest.raises(YahooAccessDenied) as caught:
        await fetch_text(session, URL, headers={})
    assert caught.value.status == status
    assert caught.value.league_not_found == (status == 403)
    assert ("no NFL league" in str(caught.value)) == (status == 403)
    assert "sign-in" not in str(caught.value)
    assert "SECRET" not in str(caught.value)
    assert len(session.calls) == 1  # No repeated requests on a refusal.


@pytest.mark.parametrize("body", ["<html>SECRET</html>", "null", "[]", '{"error":"SECRET"}', "{invalid"])
async def test_other_refusals_do_not_echo_arbitrary_body(body):
    with pytest.raises(YahooAccessDenied) as caught:
        await fetch_text(Session(Response(403, body)), URL, headers={})
    assert not caught.value.league_not_found
    assert "SECRET" not in str(caught.value)


async def test_denial_body_read_is_bounded():
    response = Response(403, "x" * 100_000)
    with pytest.raises(YahooAccessDenied):
        await fetch_text(Session(response), URL, headers={})
    assert response.read_bytes == 4096


@pytest.mark.parametrize("status", [429, 999])
async def test_a_throttled_host_is_refused_not_failed(status):
    # 999 is Yahoo's "Request denied" for a host it has throttled: the caller
    # must stop asking, which a transient failure would not tell it.
    session = Session(Response(status, "Request denied"))
    with pytest.raises(YahooRefused) as caught:
        await fetch_text(session, URL, headers={})
    assert caught.value.status == status
    assert len(session.calls) == 1


@pytest.mark.parametrize("status", [404, 500, 503])
async def test_other_http_failures_keep_their_original_classification(status):
    with pytest.raises(HttpFailure, match=str(status)):
        await fetch_text(Session(Response(status)), URL, headers={})


async def test_success_preserves_body_headers_and_timeout():
    session = Session(Response(200, '{"teams":[]}', {"Last-Modified": "yesterday"}))
    assert await fetch_text(session, URL, headers={"User-Agent": "test"}, timeout=30) == ('{"teams":[]}', "yesterday")
    assert session.calls == [(URL, {"headers": {"User-Agent": "test"}, "timeout": 30})]


async def test_conditional_response_has_no_body():
    session = Session(Response(304))
    assert await fetch_text(session, URL, headers={}, allow_not_modified=True) == (None, "")


async def test_client_preserves_access_denied_instead_of_wrapping_as_transient():
    async def fetch(url):
        body, _ = await fetch_text(Session(Response(403)), url, headers={})
        return body

    with pytest.raises(YahooAccessDenied):
        await RedzoneClient(fetch, "726304").async_fetch_seed()


COOKIE = "A1=one; T=two; Y=three"


async def test_cookie_is_sent_with_the_league_seed():
    session = Session(Response(200, "{}"))
    await fetch_text(session, URL, headers={"User-Agent": "test"}, cookie=COOKIE)
    assert session.calls[0][1]["headers"] == {"User-Agent": "test", "Cookie": COOKIE}


@pytest.mark.parametrize(
    "url",
    [
        "https://relay-stream.sports.yahoo.com/nfl/games.txt",
        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
        "http://pub-api.fantasysports.yahoo.com/fantasy/v3/redzone/nfl?league_id=726304",
        "https://pub-api.fantasysports.yahoo.com.example.com/fantasy/v3/redzone/nfl",
    ],
)
async def test_cookie_never_leaves_the_league_host(url):
    headers = {"User-Agent": "test"}
    session = Session(Response(200, "{}"))
    await fetch_text(session, url, headers=headers, cookie=COOKIE)
    assert session.calls[0][1]["headers"] == {"User-Agent": "test"}
    assert headers == {"User-Agent": "test"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("   ", ""),
        (COOKIE, COOKIE),
        (f"Cookie: {COOKIE}", COOKIE),
        (f"  cookie:{COOKIE}\n", COOKIE),
        ("A1=one;\n T=two;\r\n Y=three", COOKIE),
    ],
)
def test_pasted_cookie_is_normalized(raw, expected):
    assert normalize_cookie(raw) == expected


@pytest.mark.parametrize("raw", ["not a cookie", "A1=one\x00", "A1=one\x7f"])
def test_text_that_cannot_be_a_cookie_is_refused(raw):
    with pytest.raises(ValueError):
        normalize_cookie(raw)


def _wrapped(inner):
    try:
        try:
            raise inner
        except Exception as err:
            raise FetchFailed("no fresh data for 901s") from err
    except FetchFailed as outer:
        return outer


@pytest.mark.parametrize(
    "err",
    [YahooAccessDenied(403, league_not_found=True), _wrapped(YahooAccessDenied(403, league_not_found=True))],
)
def test_a_league_refusal_is_recognized_even_past_the_stale_window(err):
    assert league_refused(err)


@pytest.mark.parametrize("err", [YahooAccessDenied(403), FetchFailed("timeout"), _wrapped(FetchFailed("x")), None])
def test_other_failures_are_not_a_refused_league(err):
    assert not league_refused(err)
