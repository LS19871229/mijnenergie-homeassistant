"""Statussensoren: verbinding, laatste verzending, wachtrij en gekoppelde woning."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MijnEnergieConfigEntry
from .const import CONF_HOME_NAME, CONF_URL, DOMAIN, STATUSES, signal_update
from .sender import MijnEnergieSender


@dataclass(frozen=True, kw_only=True)
class MijnEnergieSensorDescription(SensorEntityDescription):
    value_fn: Callable[[MijnEnergieSender], Any]


SENSORS: tuple[MijnEnergieSensorDescription, ...] = (
    MijnEnergieSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=STATUSES,
        value_fn=lambda s: s.status,
    ),
    MijnEnergieSensorDescription(
        key="last_sent",
        translation_key="last_sent",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.last_sent,
    ),
    MijnEnergieSensorDescription(
        key="queue",
        translation_key="queue",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: len(s.queue),
    ),
    MijnEnergieSensorDescription(
        key="home",
        translation_key="home",
        value_fn=lambda s: s.entry.data.get(CONF_HOME_NAME),
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: MijnEnergieConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    sender = entry.runtime_data
    async_add_entities(MijnEnergieSensor(sender, description) for description in SENSORS)


class MijnEnergieSensor(SensorEntity):
    """Eén statussensor."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description: MijnEnergieSensorDescription

    def __init__(self, sender: MijnEnergieSender, description: MijnEnergieSensorDescription) -> None:
        self.entity_description = description
        self._sender = sender
        entry = sender.entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Mijn Energie · {entry.data.get(CONF_HOME_NAME, entry.title)}",
            manufacturer="EL trading",
            model="Mijn Energie",
            sw_version=sender.version,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=entry.data.get(CONF_URL),
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self._sender)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "status":
            return None
        s = self._sender
        since: datetime | None = s.offline_since
        return {"last_error": s.last_error, "offline_since": since.isoformat() if since else None, "config_rev": s.config_rev}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_update(self._sender.entry.entry_id), self._update)
        )

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()
