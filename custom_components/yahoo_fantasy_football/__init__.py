"""The Yahoo Fantasy Football integration.

Scaffold release. The Lovelace card bundle is already wired end to end (copied
into ``www/community/`` and self-registered as a Lovelace resource); the Yahoo
API client, coordinator and scoring-play engine land in later milestones.
"""

from __future__ import annotations

import logging
import shutil
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

# ``/hacsfiles/<name>/`` is mapped by HACS to ``<config>/www/community/<name>/``.
HACS_URL_BASE = f"/hacsfiles/{DOMAIN}/{CARD_FILENAME}"
# Fallback served straight from this package directory when the copy fails.
LEGACY_URL_BASE = f"/{DOMAIN}/{CARD_FILENAME}"


def _sync_card_to_www_community(config_path, source: Path) -> Path | None:
    """Copy the card into ``<config>/www/community/<DOMAIN>/``.

    Returns the destination path, or ``None`` if the copy failed (in which case
    the caller registers a static path instead). Runs in the executor — this is
    blocking file I/O.
    """
    try:
        target_dir = Path(config_path("www")) / "community" / DOMAIN
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / CARD_FILENAME

        # Skip the copy when the destination already matches, so a restart
        # doesn't rewrite the file (and bust every browser cache) needlessly.
        if target.exists():
            src_stat = source.stat()
            dst_stat = target.stat()
            if src_stat.st_size == dst_stat.st_size and int(src_stat.st_mtime) == int(dst_stat.st_mtime):
                return target

        shutil.copy2(source, target)
        return target
    except OSError as err:
        _LOGGER.debug("Could not sync card to www/community: %s", err)
        return None


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the bundled Lovelace card."""
    integration = await async_get_integration(hass, DOMAIN)
    version = str(integration.version)

    source = Path(__file__).parent / CARD_FILENAME
    target = await hass.async_add_executor_job(_sync_card_to_www_community, hass.config.path, source)

    if target is not None:
        card_url_base = HACS_URL_BASE
    else:
        card_url_base = LEGACY_URL_BASE
        # Only register the static path when we actually need it — avoids
        # exposing the integration's package directory over HTTP otherwise.
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    url_path=f"/{DOMAIN}",
                    path=str(Path(__file__).parent),
                    cache_headers=False,
                )
            ]
        )

    card_url = f"{card_url_base}?v={version}"
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

        def _is_ours(url: str) -> bool:
            return HACS_URL_BASE in url or LEGACY_URL_BASE in url

        existing = [r for r in resources.async_items() if _is_ours(r.get("url", ""))]
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
