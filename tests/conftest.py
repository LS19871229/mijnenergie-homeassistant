"""Gedeelde testopzet."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mijnenergie.const import (
    CONF_BATTERY_INVERT,
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_GAS,
    CONF_HOME_ID,
    CONF_HOME_MODE,
    CONF_HOME_NAME,
    CONF_P1_EXPORT,
    CONF_P1_IMPORT,
    CONF_P1_POWER,
    CONF_PV_ENERGY,
    CONF_PV_POWER,
    CONF_TOKEN,
    CONF_URL,
)

PAIR_OK: dict[str, Any] = {"ok": True, "token": "ep_testtoken", "home": {"id": 12, "name": "Fam. de Vries · Zwolle", "mode": "pv"}}

ENTRY_DATA = {
    CONF_URL: "https://eltrading.nl/mijnenergie",
    CONF_TOKEN: "ep_testtoken",
    CONF_HOME_ID: 12,
    CONF_HOME_NAME: "Fam. de Vries · Zwolle",
    CONF_HOME_MODE: "pv",
}
ENTRY_OPTIONS = {
    CONF_P1_POWER: "sensor.p1_meter_power",
    CONF_P1_IMPORT: "sensor.p1_meter_energy_import",
    CONF_P1_EXPORT: "sensor.p1_meter_energy_export",
    CONF_GAS: "sensor.p1_meter_total_gas",
    CONF_PV_POWER: "sensor.solaredge_current_power",
    CONF_PV_ENERGY: "sensor.solaredge_lifetime_energy",
    CONF_BATTERY_POWER: "",
    CONF_BATTERY_SOC: "",
    CONF_BATTERY_INVERT: False,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Custom integrations laden in elke test."""
    return


@pytest.fixture(autouse=True)
def no_minute_timer():
    """De echte klok mag tijdens een test geen extra meting starten (anders kloppen tellingen niet).

    Tests roepen sender.async_tick() zelf aan; de start-meting na 5 seconden blijft actief.
    """
    with patch("custom_components.mijnenergie.sender.async_track_time_change", return_value=lambda: None):
        yield


def _register(hass: HomeAssistant, platform: str, unique_id: str, object_id: str, device_id: str | None,
              config_entry: MockConfigEntry, state: str, attrs: dict[str, Any]) -> str:
    entry = er.async_get(hass).async_get_or_create(
        "sensor", platform, unique_id, suggested_object_id=object_id, device_id=device_id, config_entry=config_entry,
        original_device_class=attrs.get("device_class"),
    )
    hass.states.async_set(entry.entity_id, state, attrs)
    return entry.entity_id


@pytest.fixture
def meters(hass: HomeAssistant) -> dict[str, str]:
    """Een HomeWizard P1-meter en een SolarEdge-omvormer, zoals op een echte HA Green."""
    hw = MockConfigEntry(domain="homewizard")
    hw.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=hw.entry_id, identifiers={("homewizard", "ABC")}, name="P1 meter")
    ids = {
        CONF_P1_POWER: _register(hass, "homewizard", "ABC_active_power_w", "p1_meter_power", device.id, hw, "-720",
                                 {"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"}),
        CONF_P1_IMPORT: _register(hass, "homewizard", "ABC_total_power_import_kwh", "p1_meter_energy_import", device.id, hw, "4123.512",
                                  {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"}),
        CONF_P1_EXPORT: _register(hass, "homewizard", "ABC_total_power_export_kwh", "p1_meter_energy_export", device.id, hw, "2210.044",
                                  {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"}),
        CONF_GAS: _register(hass, "homewizard", "ABC_total_gas_m3", "p1_meter_total_gas", device.id, hw, "1532.118",
                            {"unit_of_measurement": "m³", "device_class": "gas", "state_class": "total_increasing"}),
    }
    se = MockConfigEntry(domain="solaredge")
    se.add_to_hass(hass)
    ids[CONF_PV_POWER] = _register(hass, "solaredge", "SE1_current_power", "solaredge_current_power", None, se, "1.24",
                                   {"unit_of_measurement": "kW", "device_class": "power", "state_class": "measurement"})
    ids[CONF_PV_ENERGY] = _register(hass, "solaredge", "SE1_lifetime_energy", "solaredge_lifetime_energy", None, se, "9876300",
                                    {"unit_of_measurement": "Wh", "device_class": "energy", "state_class": "total_increasing"})
    # Een afleider: opbrengst van dit jaar
    _register(hass, "solaredge", "SE1_energy_this_year", "solaredge_energy_this_year", None, se, "3890000",
              {"unit_of_measurement": "Wh", "device_class": "energy", "state_class": "total_increasing"})
    return ids
