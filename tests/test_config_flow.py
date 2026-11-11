"""Run the actual config flow with a minimal HA shell and fake HTTP responses.

These tests cover the HTTP-to-form contract, not a full HA installation.
"""

import importlib.util
import json
import sys
from types import ModuleType
from typing import ClassVar

import pytest
from conftest import FIXTURES, REPO_ROOT
from http_fakes import Response, Session


class ConfigFlowShell:
    """Only the HA form/entry API used by this integration."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self):
        return None

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def _get_reauth_entry(self):
        return self.reauth_entry

    def async_update_reload_and_abort(self, entry, *, data_updates):
        return {"type": "abort", "reason": "reauth_successful", "entry": entry, "data_updates": data_updates}


@pytest.fixture
def flow_factory(monkeypatch):
    import homeassistant.config_entries as entries

    monkeypatch.setattr(entries, "ConfigFlow", ConfigFlowShell, raising=False)
    results = ModuleType("homeassistant.data_entry_flow")
    results.FlowResult = dict
    monkeypatch.setitem(sys.modules, results.__name__, results)
    http = ModuleType("homeassistant.helpers.aiohttp_client")
    monkeypatch.setitem(sys.modules, http.__name__, http)
    spec = importlib.util.spec_from_file_location(
        "yahoo_fantasy_football._config_flow_test",
        REPO_ROOT / "custom_components/yahoo_fantasy_football/config_flow.py",
    )

    def create(response):
        session = Session(response)
        http.async_get_clientsession = lambda hass: session
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        flow = module.YahooFantasyFootballConfigFlow()
        flow.hass = object()
        return flow, session

    return create


@pytest.mark.parametrize("league", ["726304", "https://football.fantasysports.yahoo.com/f1/913531", "99999999"])
async def test_missing_league_has_actionable_form_error(flow_factory, league):
    flow, session = flow_factory(Response(403, json.dumps({"error": {"description": "Unable to retrieve cookie."}})))
    result = await flow.async_step_user({"league_id": league})
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "league_not_found"}
    assert len(session.calls) == 1


async def test_server_failure_keeps_generic_connection_error(flow_factory):
    flow, _ = flow_factory(Response(503))
    result = await flow.async_step_user({"league_id": "726304"})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_invalid_id_never_makes_http_request(flow_factory):
    flow, session = flow_factory(Response())
    result = await flow.async_step_user({"league_id": "not a league"})
    assert result["errors"] == {"base": "invalid_league_id"}
    assert session.calls == []


async def test_success_still_offers_real_teams_and_creates_entry(flow_factory):
    flow, session = flow_factory(Response(200, (FIXTURES / "yahoo_redzone_2026_w1.json").read_text()))
    result = await flow.async_step_user({"league_id": "999999", "name": "My league"})
    assert result["step_id"] == "team"
    assert flow._teams
    team_id = next(iter(flow._teams))
    entry = await flow.async_step_team({"team_id": team_id})
    assert entry["type"] == "create_entry"
    assert entry["data"] == {"league_id": "999999", "name": "My league", "team_id": team_id}
    assert len(session.calls) == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_other_refusals_keep_generic_access_error(flow_factory, status):
    flow, _ = flow_factory(Response(status, "<html>Do not expose this response</html>"))
    result = await flow.async_step_user({"league_id": "802904"})
    assert result["errors"] == {"base": "yahoo_access_denied"}


SEED = (FIXTURES / "yahoo_redzone_2026_w1.json").read_text()
DENIED = json.dumps({"error": {"description": "Unable to retrieve cookie."}})
COOKIE = "A1=one; T=two; Y=three"


async def test_private_league_is_read_with_the_cookie_and_keeps_it(flow_factory):
    flow, session = flow_factory(Response(200, SEED))
    result = await flow.async_step_user({"league_id": "999999", "cookie": f"Cookie: {COOKIE}\n"})
    assert result["step_id"] == "team"
    assert session.calls[0][1]["headers"]["Cookie"] == COOKIE
    entry = await flow.async_step_team({"team_id": "none"})
    assert entry["data"]["cookie"] == COOKIE


async def test_public_league_entry_carries_no_cookie(flow_factory):
    flow, session = flow_factory(Response(200, SEED))
    await flow.async_step_user({"league_id": "999999", "cookie": "  "})
    assert "Cookie" not in session.calls[0][1]["headers"]
    entry = await flow.async_step_team({"team_id": "none"})
    assert "cookie" not in entry["data"]


async def test_a_cookie_yahoo_refuses_is_reported_as_the_cookie(flow_factory):
    flow, _ = flow_factory(Response(403, DENIED))
    result = await flow.async_step_user({"league_id": "999999", "cookie": COOKIE})
    assert result["errors"] == {"base": "cookie_rejected"}


async def test_text_that_is_not_a_cookie_never_makes_http_request(flow_factory):
    flow, session = flow_factory(Response(200, SEED))
    result = await flow.async_step_user({"league_id": "999999", "cookie": "my password"})
    assert result["errors"] == {"base": "invalid_cookie"}
    assert session.calls == []


class Entry:
    title = "Kush"
    data: ClassVar[dict] = {"league_id": "999999", "name": "Kush", "cookie": "A1=old"}


async def test_reauth_stores_a_cookie_only_once_yahoo_serves_the_league_with_it(flow_factory):
    flow, session = flow_factory(Response(200, SEED))
    flow.reauth_entry = Entry()
    form = await flow.async_step_reauth(Entry.data)
    assert form["step_id"] == "reauth_confirm"
    result = await flow.async_step_reauth_confirm({"cookie": COOKIE})
    assert result["reason"] == "reauth_successful"
    assert result["data_updates"] == {"cookie": COOKIE}
    assert session.calls[0][1]["headers"]["Cookie"] == COOKIE


async def test_reauth_keeps_asking_while_yahoo_refuses_the_cookie(flow_factory):
    flow, _ = flow_factory(Response(403, DENIED))
    flow.reauth_entry = Entry()
    result = await flow.async_step_reauth_confirm({"cookie": COOKIE})
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "cookie_rejected"}
