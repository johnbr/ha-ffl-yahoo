"""The Yahoo Fantasy Football integration.

The Lovelace card bundle ships inside this package. ``async_setup`` serves that
one file on a URL path of its own and registers it as a Lovelace resource, so
the cards load the same way whether the integration came from HACS or was
copied in by hand.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.loader import async_get_integration

from .const import CARD_FILENAME, DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

# ``async_setup`` exists purely to register the card, so declare that YAML
# configuration is not supported (hassfest requires this pairing).
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# The card file itself, served from this package on a path of its own. Only
# that one file is exposed over HTTP, never the package directory.
CARD_URL_PATH = f"/{DOMAIN}/{CARD_FILENAME}"

# Where earlier releases pointed the resource. ``/hacsfiles/`` is a route HACS
# serves, so it dangled on a manual install; the resource is moved off either
# of these on the next start.
_PREVIOUS_URL_PATHS = (f"/hacsfiles/{DOMAIN}/{CARD_FILENAME}",)


def _card_digest(source: Path) -> str:
    """Short content hash of the card bundle, for cache-busting.

    The version alone is not enough: between releases the bundle changes many
    times while ``manifest.json`` does not, so a ``?v=<version>`` URL is byte
    identical and every browser serves the **cached, stale** card. That is
    invisible from the server side — the file on disk is correct and the page
    is wrong — and it costs a hard refresh to notice.

    Hashing the content means the URL changes exactly when the card does.
    """
    try:
        return hashlib.sha256(source.read_bytes()).hexdigest()[:8]
    except OSError:
        return ""


def card_resource_url(version: str, digest: str) -> str:
    """The Lovelace resource URL: the card's path plus its cache-buster."""
    return f"{CARD_URL_PATH}?v={version}.{digest}" if digest else f"{CARD_URL_PATH}?v={version}"


def is_card_resource(url: str) -> bool:
    """Whether a Lovelace resource URL is this card's, current or previous."""
    # By suffix, so a resource someone added with a full origin still matches.
    path = url.split("?", 1)[0]
    return any(path.endswith(known) for known in (CARD_URL_PATH, *_PREVIOUS_URL_PATHS))


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Serve and register the bundled Lovelace card."""
    integration = await async_get_integration(hass, DOMAIN)
    version = str(integration.version)

    source = Path(__file__).parent / CARD_FILENAME
    digest = await hass.async_add_executor_job(_card_digest, source)

    # Cache headers are safe: the resource URL changes whenever the file does.
    await hass.http.async_register_static_paths(
        [StaticPathConfig(url_path=CARD_URL_PATH, path=str(source), cache_headers=True)]
    )

    card_url = card_resource_url(version, digest)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["_card_url"] = card_url

    # Try now, and again once HA has fully started — Lovelace may not be ready
    # during ``async_setup``.
    await _async_register_card(hass, card_url)

    async def _register_on_start(_event) -> None:
        await _async_register_card(hass, card_url)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _register_on_start)

    return True


async def _async_register_card(hass: HomeAssistant, card_url: str) -> None:
    """Create or update the card's Lovelace resource entry (idempotent)."""
    try:
        from homeassistant.components.lovelace.const import DOMAIN as LOVELACE_DOMAIN

        lovelace_data = hass.data.get(LOVELACE_DOMAIN)
        if lovelace_data is None:
            _LOGGER.debug("Lovelace not ready yet")
            return

        resources = getattr(lovelace_data, "resources", None)
        if resources is None:
            _LOGGER.debug("Lovelace resources not available (YAML resource mode?)")
            _LOGGER.info("Add this resource manually: %s (type: module)", card_url)
            return

        if hasattr(resources, "loaded") and not resources.loaded:
            await resources.async_load()
            resources.loaded = True

        existing = [r for r in resources.async_items() if is_card_resource(r.get("url", ""))]
        if existing:
            for res in existing:
                if res.get("url") != card_url:
                    _LOGGER.info(
                        "Updating Yahoo Fantasy Football card resource: %s -> %s",
                        res.get("url"),
                        card_url,
                    )
                    await resources.async_update_item(res["id"], {"url": card_url})
            return

        await resources.async_create_item({"url": card_url, "res_type": "module"})
        _LOGGER.info("Registered Yahoo Fantasy Football card as Lovelace resource: %s", card_url)
    except ImportError:
        _LOGGER.debug("Lovelace resources module not available")
    # Never break setup over a card resource — log and tell the user how to add it.
    except Exception as err:
        _LOGGER.warning("Could not auto-register card resource: %s", err)
        _LOGGER.info("Manually add this resource: %s (type: module)", card_url)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a league from a config entry."""
    from .coordinator import YahooFantasyCoordinator
    from .websocket import async_register_commands

    async_register_commands(hass)

    coordinator = YahooFantasyCoordinator(hass, entry)
    await coordinator.async_load_history()
    # Raises ConfigEntryNotReady on a transient failure, so HA retries setup;
    # a private league surfaces as a permanent, explained failure instead.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {**entry.data, "coordinator": coordinator}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
