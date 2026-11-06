"""Diagnosebestand voor support (zonder token)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import MijnEnergieConfigEntry
from .const import CONF_TOKEN, SENSOR_KINDS

TO_REDACT = {CONF_TOKEN}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: MijnEnergieConfigEntry) -> dict[str, Any]:
    sender = entry.runtime_data
    sensors = {}
    for key, entity_id in entry.options.items():
        if key in SENSOR_KINDS and entity_id:
            st = hass.states.get(entity_id)
            sensors[key] = {
                "entity_id": entity_id,
                "state": st.state if st else None,
                "unit": st.attributes.get("unit_of_measurement") if st else None,
                "device_class": st.attributes.get("device_class") if st else None,
                "state_class": st.attributes.get("state_class") if st else None,
            }
    return {
        "entry": {"data": async_redact_data(dict(entry.data), TO_REDACT), "options": dict(entry.options)},
        "version": sender.version,
        "status": sender.status,
        "last_sent": sender.last_sent.isoformat() if sender.last_sent else None,
        "last_error": sender.last_error,
        "offline_since": sender.offline_since.isoformat() if sender.offline_since else None,
        "queue_length": len(sender.queue),
        "oldest_in_queue": sender.queue[0]["ts"] if sender.queue else None,
        "last_measurement": sender.last_measurement,
        "sensors": sensors,
    }
