"""HTTP contracts: status/body handling, confidentiality and conditional fetches."""

import json

import pytest
from http_fakes import HttpFailure, Response, Session
from yahoo_fantasy_football.http_client import YahooAccessDenied, fetch_text
from yahoo_fantasy_football.redzone_client import RedzoneClient

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


@pytest.mark.parametrize("status", [404, 429, 500, 503])
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
