"""Run the actual config flow with a minimal HA shell and fake HTTP responses.

These tests cover the HTTP-to-form contract, not a full HA installation.
"""

import importlib.util
import json
import sys
from types import ModuleType

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


@pytest.mark.parametrize("league", ["726304", "https://football.fantasysports.yahoo.com/f1/913531"])
async def test_yahoo_cookie_refusal_has_actionable_form_error(flow_factory, league):
    flow, session = flow_factory(Response(403, json.dumps({"error": {"description": "Unable to retrieve cookie."}})))
    result = await flow.async_step_user({"league_id": league})
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "yahoo_access_denied"}
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
