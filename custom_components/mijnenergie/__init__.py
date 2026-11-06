"""Mijn Energie: stuurt metingen van P1-meter en omvormer naar het portaal van EL trading."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_integration

from .api import MijnEnergieApi
from .const import CONF_TOKEN, CONF_URL, DOMAIN, SENSOR_KINDS
from .sender import MijnEnergieSender, queue_store

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

type MijnEnergieConfigEntry = ConfigEntry[MijnEnergieSender]


async def async_setup_entry(hass: HomeAssistant, entry: MijnEnergieConfigEntry) -> bool:
    """Start het versturen voor één gekoppelde woning."""
    integration = await async_get_integration(hass, DOMAIN)
    api = MijnEnergieApi(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN])
    sender = MijnEnergieSender(hass, entry, api, str(integration.version or "0"))
    await sender.async_start()
    entry.runtime_data = sender

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    @callback
    def _entity_renamed(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        """Hernoemt iemand een gekozen sensor? Dan de keuze automatisch meenemen."""
        data = event.data
        if data["action"] != "update" or "old_entity_id" not in data:
            return
        old, new = data["old_entity_id"], data["entity_id"]
        changed = {k: new for k, v in entry.options.items() if k in SENSOR_KINDS and v == old}
        if changed:
            _LOGGER.info("Mijn Energie: sensor %s heet nu %s; keuze bijgewerkt", old, new)
            hass.config_entries.async_update_entry(entry, options={**entry.options, **changed})

    entry.async_on_unload(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _entity_renamed))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: MijnEnergieConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MijnEnergieConfigEntry) -> bool:
    """Stoppen; de wachtrij wordt opgeslagen."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Integratie verwijderd: wachtrij en meldingen opruimen."""
    await queue_store(hass, entry.entry_id).async_remove()
    for issue_id in list(ir.async_get(hass).issues):
        if issue_id[0] == DOMAIN and entry.entry_id in issue_id[1]:
            ir.async_delete_issue(hass, DOMAIN, issue_id[1])
