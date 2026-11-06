"""Voorstellen doen voor de juiste sensoren (P1, omvormer, batterij).

Alleen voorstellen: in elk scherm kan de installateur zelf een andere sensor kiezen.
"""

from __future__ import annotations

from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_GAS,
    CONF_P1_EXPORT,
    CONF_P1_IMPORT,
    CONF_P1_POWER,
    CONF_PV_ENERGY,
    CONF_PV_POWER,
    DOMAIN,
)
from .measure import FACTORS

HOMEWIZARD = "homewizard"

# HomeWizard gebruikt unique_id = "<serienummer>_<sleutel>"
HW_KEYS = {
    CONF_P1_POWER: ("_active_power_w",),
    CONF_P1_IMPORT: ("_total_power_import_kwh",),
    CONF_P1_EXPORT: ("_total_power_export_kwh",),
}


def _sensor_entries(hass: HomeAssistant) -> list[er.RegistryEntry]:
    reg = er.async_get(hass)
    return [e for e in reg.entities.values() if e.domain == "sensor" and not e.disabled_by and e.platform != DOMAIN]


def _attrs(hass: HomeAssistant, entity_id: str) -> tuple[str | None, str | None, str | None]:
    """(device_class, unit, state_class) uit de actuele toestand."""
    st = hass.states.get(entity_id)
    if st is None:
        return None, None, None
    return st.attributes.get(ATTR_DEVICE_CLASS), st.attributes.get(ATTR_UNIT_OF_MEASUREMENT), st.attributes.get("state_class")


def suggest_p1(hass: HomeAssistant) -> tuple[dict[str, str], str | None]:
    """Sensoren van een HomeWizard P1-meter. Geeft (voorstellen, apparaatnaam)."""
    entries = [e for e in _sensor_entries(hass) if e.platform == HOMEWIZARD]
    by_device: dict[str | None, list[er.RegistryEntry]] = {}
    for e in entries:
        by_device.setdefault(e.device_id, []).append(e)

    best: dict[str, str] = {}
    best_device: str | None = None
    for device_id, ents in by_device.items():
        found: dict[str, str] = {}
        for key, suffixes in HW_KEYS.items():
            for e in ents:
                if any((e.unique_id or "").endswith(s) for s in suffixes):
                    found[key] = e.entity_id
                    break
        if len(found) > len(best):
            best, best_device = found, device_id

    # Gas: HomeWizard-sensor met device_class gas (soms als apart apparaat)
    for e in entries:
        dc, unit, _ = _attrs(hass, e.entity_id)
        if (dc == "gas" or e.original_device_class == "gas") and unit in FACTORS["gas"]:
            best[CONF_GAS] = e.entity_id
            break

    name = None
    if best_device:
        device = dr.async_get(hass).async_get(best_device)
        if device:
            name = device.name_by_user or device.name
    return best, name


_PV_GOOD = ("solar", "pv", "inverter", "omvormer", "yield", "opbrengst", "production", "productie",
            "solaredge", "growatt", "solis", "goodwe", "huawei", "sma", "enphase", "envoy", "fronius", "sunny")
_PV_BAD = ("battery", "batterij", "grid", "import", "export", "consumption", "verbruik", "load",
           "today", "vandaag", "daily", "day", "week", "month", "maand", "year", "jaar", "p1")


def _score(entity_id: str, name: str, good: tuple[str, ...], bad: tuple[str, ...]) -> int:
    text = f"{entity_id} {name}".lower()
    score = sum(3 for g in good if g in text) - sum(5 for b in bad if b in text)
    return score


def suggest_pv(hass: HomeAssistant) -> dict[str, str]:
    """Beste gok voor omvormervermogen en totale opbrengst (niet van HomeWizard P1)."""
    power: list[tuple[int, str]] = []
    energy: list[tuple[int, str]] = []
    for e in _sensor_entries(hass):
        if e.platform == HOMEWIZARD:
            continue
        dc, unit, sc = _attrs(hass, e.entity_id)
        dc = dc or e.original_device_class
        name = e.name or e.original_name or ""
        if dc == "power" and unit in FACTORS["power"]:
            power.append((_score(e.entity_id, name, _PV_GOOD + ("current", "ac", "output", "active"), _PV_BAD), e.entity_id))
        elif dc == "energy" and unit in FACTORS["energy"] and sc in ("total_increasing", "total", None):
            energy.append((_score(e.entity_id, name, _PV_GOOD + ("lifetime", "total", "totaal"), _PV_BAD), e.entity_id))
    out: dict[str, str] = {}
    if power:
        s, eid = max(power)
        if s > 0:
            out[CONF_PV_POWER] = eid
    if energy:
        s, eid = max(energy)
        if s > 0:
            out[CONF_PV_ENERGY] = eid
    return out


def suggest_battery(hass: HomeAssistant) -> dict[str, str]:
    """Beste gok voor batterijvermogen en laadpercentage."""
    out: dict[str, str] = {}
    power: list[tuple[int, str]] = []
    soc: list[tuple[int, str]] = []
    for e in _sensor_entries(hass):
        dc, unit, _ = _attrs(hass, e.entity_id)
        dc = dc or e.original_device_class
        text = f"{e.entity_id} {e.name or e.original_name or ''}".lower()
        if "batter" not in text:
            continue
        if dc == "power" and unit in FACTORS["power"]:
            power.append((_score(e.entity_id, text, ("charge", "laad", "power"), ("max", "limit")), e.entity_id))
        elif dc == "battery" and unit == "%":
            soc.append((_score(e.entity_id, text, ("soc", "state_of_charge", "level", "capacity"), ("health", "soh")), e.entity_id))
    if power:
        out[CONF_BATTERY_POWER] = max(power)[1]
    if soc:
        out[CONF_BATTERY_SOC] = max(soc)[1]
    return out
