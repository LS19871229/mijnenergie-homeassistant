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
    CONF_CONFIG,
    CONF_CONFIG_DIRTY,
    CONF_CONFIG_REV,
    CONF_HOME_ID,
    CONF_HOME_MODE,
    CONF_HOME_NAME,
    CONF_TOKEN,
    CONF_URL,
)

API = "custom_components.mijnenergie.api.MijnEnergieApi"

PAIR_OK: dict[str, Any] = {"ok": True, "token": "ep_testtoken", "home": {"id": 12, "name": "Fam. de Vries · Zwolle", "mode": "pv"},
                           "config_rev": 0, "config": None}

ENTRY_DATA = {
    CONF_URL: "https://eltrading.nl/mijnenergie",
    CONF_TOKEN: "ep_testtoken",
    CONF_HOME_ID: 12,
    CONF_HOME_NAME: "Fam. de Vries · Zwolle",
    CONF_HOME_MODE: "pv",
}

CONFIG_INVERTER = {
    "installation": "inverter",
    "p1": {"power": "sensor.p1_meter_power", "import": "sensor.p1_meter_energy_import",
           "export": "sensor.p1_meter_energy_export", "gas": "sensor.p1_meter_total_gas"},
    "pv": [{"label": "", "power": "sensor.solaredge_current_power", "energy": "sensor.solaredge_lifetime_energy"}],
    "battery": [],
    "house": {"power": ""},
}
ENTRY_OPTIONS = {CONF_CONFIG: CONFIG_INVERTER, CONF_CONFIG_REV: 3, CONF_CONFIG_DIRTY: False}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Custom integrations laden in elke test."""
    return


@pytest.fixture(autouse=True)
def no_wall_clock_timers():
    """De echte klok mag tijdens een test geen extra meting of sensorlijst starten.

    Tests roepen sender.async_tick() / async_send_inventory() zelf aan; de start-meting na 5 s blijft actief.
    """
    with patch("custom_components.mijnenergie.sender.async_track_time_change", return_value=lambda: None), \
         patch("custom_components.mijnenergie.sender.async_track_time_interval", return_value=lambda: None):
        yield


def _register(hass: HomeAssistant, platform: str, unique_id: str, object_id: str, device_id: str | None,
              config_entry: MockConfigEntry, state: str, attrs: dict[str, Any]) -> str:
    entry = er.async_get(hass).async_get_or_create(
        "sensor", platform, unique_id, suggested_object_id=object_id, device_id=device_id, config_entry=config_entry,
        original_device_class=attrs.get("device_class"),
    )
    hass.states.async_set(entry.entity_id, state, attrs)
    return entry.entity_id


W = {"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"}
KW = {"unit_of_measurement": "kW", "device_class": "power", "state_class": "measurement"}
KWH = {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"}
WH = {"unit_of_measurement": "Wh", "device_class": "energy", "state_class": "total_increasing"}
PCT = {"unit_of_measurement": "%", "device_class": "battery", "state_class": "measurement"}


@pytest.fixture
def meters(hass: HomeAssistant) -> dict[str, str]:
    """HomeWizard P1-meter en een SolarEdge-omvormer (zonder batterij)."""
    hw = MockConfigEntry(domain="homewizard")
    hw.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=hw.entry_id, identifiers={("homewizard", "ABC")}, name="P1 meter")
    ids = {
        "p1_power": _register(hass, "homewizard", "ABC_active_power_w", "p1_meter_power", device.id, hw, "-720", W),
        "p1_import": _register(hass, "homewizard", "ABC_total_power_import_kwh", "p1_meter_energy_import", device.id, hw, "4123.512", KWH),
        "p1_export": _register(hass, "homewizard", "ABC_total_power_export_kwh", "p1_meter_energy_export", device.id, hw, "2210.044", KWH),
        "gas": _register(hass, "homewizard", "ABC_total_gas_m3", "p1_meter_total_gas", device.id, hw, "1532.118",
                         {"unit_of_measurement": "m³", "device_class": "gas", "state_class": "total_increasing"}),
    }
    se = MockConfigEntry(domain="solaredge")
    se.add_to_hass(hass)
    ids["pv_power"] = _register(hass, "solaredge", "SE1_current_power", "solaredge_current_power", None, se, "1.24", KW)
    ids["pv_energy"] = _register(hass, "solaredge", "SE1_lifetime_energy", "solaredge_lifetime_energy", None, se, "9876300", WH)
    _register(hass, "solaredge", "SE1_energy_this_year", "solaredge_energy_this_year", None, se, "3890000", WH)  # afleider
    return ids


@pytest.fixture
def hybrid(hass: HomeAssistant, meters: dict[str, str]) -> dict[str, str]:
    """Daarbij een hybride Huawei-omvormer met batterij. 'Active power' is de uitgang (de valkuil)."""
    hu = MockConfigEntry(domain="huawei_solar")
    hu.add_to_hass(hass)
    ids = dict(meters)
    ids["inv_input"] = _register(hass, "huawei_solar", "HU_input_power", "inverter_input_power", None, hu, "3200", W)
    ids["inv_active"] = _register(hass, "huawei_solar", "HU_active_power", "inverter_active_power", None, hu, "1700", W)
    ids["inv_pv_total"] = _register(hass, "huawei_solar", "HU_pv_total", "inverter_pv_total_yield", None, hu, "9876.0", KWH)
    ids["inv_daily"] = _register(hass, "huawei_solar", "HU_daily", "inverter_daily_yield", None, hu, "7.8", KWH)
    ids["bat_power"] = _register(hass, "huawei_solar", "HU_bat_power", "battery_charge_discharge_power", None, hu, "1500", W)
    ids["bat_soc"] = _register(hass, "huawei_solar", "HU_bat_soc", "battery_state_of_capacity", None, hu, "55", PCT)
    ids["bat_charge"] = _register(hass, "huawei_solar", "HU_bat_charge", "battery_total_charge", None, hu, "1802.0", KWH)
    ids["bat_discharge"] = _register(hass, "huawei_solar", "HU_bat_discharge", "battery_total_discharge", None, hu, "1655.0", KWH)
    ids["house"] = _register(hass, "huawei_solar", "HU_load", "house_consumption_power", None, hu, "1010", W)
    return ids
