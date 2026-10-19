"""How the bundled card is served and registered as a Lovelace resource.

The card must load on a manual install, where nothing serves HACS's
``/hacsfiles/`` route: so it is served from the package on a path of its own,
only the card file is exposed, and nothing is written into ``www/community``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import yahoo_fantasy_football as integration
from yahoo_fantasy_football.const import CARD_FILENAME, DOMAIN

CARD_FILE = Path(integration.__file__).parent / CARD_FILENAME


class _Resources:
    """The slice of Lovelace's resource collection the integration uses."""

    loaded = True

    def __init__(self, items: list[dict]) -> None:
        self.items = items

    def async_items(self) -> list[dict]:
        return self.items

    async def async_create_item(self, data: dict) -> None:
        self.items.append({"id": str(len(self.items) + 1), **data})

    async def async_update_item(self, item_id: str, data: dict) -> None:
        for item in self.items:
            if item["id"] == item_id:
                item.update(data)


class _Hass:
    def __init__(self, tmp_path: Path, resources: _Resources) -> None:
        self.served: list = []
        self.data = {"lovelace": SimpleNamespace(resources=resources)}
        self.config = SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts)))
        self.http = SimpleNamespace(async_register_static_paths=self._register)
        self.bus = SimpleNamespace(async_listen_once=lambda *_: None)

    async def _register(self, configs: list) -> None:
        self.served.extend(configs)

    async def async_add_executor_job(self, func, *args):
        return func(*args)


async def _setup(monkeypatch, tmp_path: Path, items: list[dict]) -> tuple[_Hass, _Resources]:
    async def _get_integration(_hass, _domain):
        return SimpleNamespace(version="1.2.3")

    monkeypatch.setattr(integration, "async_get_integration", _get_integration)
    # ``_async_register_card`` imports Lovelace's DOMAIN lazily.
    const = ModuleType("homeassistant.components.lovelace.const")
    const.DOMAIN = "lovelace"
    monkeypatch.setitem(sys.modules, "homeassistant.components.lovelace", ModuleType("lovelace"))
    monkeypatch.setitem(sys.modules, "homeassistant.components.lovelace.const", const)

    resources = _Resources(items)
    hass = _Hass(tmp_path, resources)
    assert await integration.async_setup(hass, {})
    return hass, resources


async def test_serves_only_the_card_file_on_its_own_path(monkeypatch, tmp_path) -> None:
    hass, _ = await _setup(monkeypatch, tmp_path, [])

    assert len(hass.served) == 1
    served = hass.served[0]
    assert served.url_path == f"/{DOMAIN}/{CARD_FILENAME}"
    assert Path(served.path) == CARD_FILE
    assert Path(served.path).is_file()


async def test_writes_nothing_into_the_config_directory(monkeypatch, tmp_path) -> None:
    await _setup(monkeypatch, tmp_path, [])

    assert not (tmp_path / "www").exists()


async def test_registers_the_served_url_with_a_cache_buster(monkeypatch, tmp_path) -> None:
    hass, resources = await _setup(monkeypatch, tmp_path, [])

    [item] = resources.items
    path, _, query = item["url"].partition("?")
    assert path == hass.served[0].url_path
    assert query.startswith("v=1.2.3.")
    assert item["res_type"] == "module"


async def test_moves_an_existing_hacsfiles_resource_onto_the_served_url(monkeypatch, tmp_path) -> None:
    old = {"id": "7", "url": f"/hacsfiles/{DOMAIN}/{CARD_FILENAME}?v=0.24.2.abcd1234", "res_type": "module"}
    other = {"id": "8", "url": "/hacsfiles/button-card/button-card.js", "res_type": "module"}
    hass, resources = await _setup(monkeypatch, tmp_path, [old, other])

    assert len(resources.items) == 2
    assert old["url"].startswith(f"{hass.served[0].url_path}?v=1.2.3.")
    assert other["url"] == "/hacsfiles/button-card/button-card.js"


def test_resource_matching_is_by_path_not_query() -> None:
    assert integration.is_card_resource(f"/{DOMAIN}/{CARD_FILENAME}?v=1")
    assert integration.is_card_resource(f"https://ha.example/hacsfiles/{DOMAIN}/{CARD_FILENAME}")
    assert not integration.is_card_resource(f"/other.js?v={DOMAIN}/{CARD_FILENAME}")
    assert not integration.is_card_resource("/hacsfiles/button-card/button-card.js")
