"""Voorstellen doen voor de juiste sensoren (P1, omvormer, batterij, huis).

Alleen voorstellen: in elk scherm (en in het portaal) kan de installateur zelf een andere sensor kiezen.
Sleutels in de resultaten zijn de velden van de configuratie ("power", "import", ...).
"""

from __future__ import annotations

from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .measure import FACTORS

HOMEWIZARD = "homewizard"

# HomeWizard gebruikt unique_id = "<serienummer>_<sleutel>"
HW_KEYS = {
    "power": ("_active_power_w",),
    "import": ("_total_power_import_kwh",),
    "export": ("_total_power_export_kwh",),
}

_BAD_PERIOD = ("today", "vandaag", "daily", "day", "week", "month", "maand", "year", "jaar")
_BATTERY = ("batter", "luna", "bms")


def _sensor_entries(hass: HomeAssistant) -> list[er.RegistryEntry]:
    reg = er.async_get(hass)
    return [e for e in reg.entities.values() if e.domain == "sensor" and not e.disabled_by and e.platform != DOMAIN]


def _attrs(hass: HomeAssistant, e: er.RegistryEntry) -> tuple[str | None, str | None, str | None]:
    """(device_class, unit, state_class) uit de actuele toestand."""
    st = hass.states.get(e.entity_id)
    if st is None:
        return e.original_device_class, None, None
    return (st.attributes.get(ATTR_DEVICE_CLASS) or e.original_device_class,
            st.attributes.get(ATTR_UNIT_OF_MEASUREMENT), st.attributes.get("state_class"))


def _text(e: er.RegistryEntry) -> str:
    return f"{e.entity_id} {e.name or e.original_name or ''}".lower()


def _count(text: str, words: tuple[str, ...]) -> int:
    return sum(1 for w in words if w in text)


def _best(cands: list[tuple[int, str]]) -> str | None:
    if not cands:
        return None
    score, entity_id = max(cands)
    return entity_id if score > 0 else None


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

    for e in entries:
        dc, unit, _ = _attrs(hass, e)
        if dc == "gas" and unit in FACTORS["gas"]:
            best["gas"] = e.entity_id
            break

    name = None
    if best_device:
        device = dr.async_get(hass).async_get(best_device)
        if device:
            name = device.name_by_user or device.name
    return best, name


def suggest_pv(hass: HomeAssistant, hybrid: bool = False, exclude: set[str] | None = None) -> dict[str, str]:
    """Beste gok voor PV-vermogen en totale PV-opbrengst (niet van de P1-meter of batterij).

    Bij een hybride omvormer zoeken we het vermogen van de panelen (input/DC/PV), niet de uitgang.
    """
    exclude = exclude or set()
    power: list[tuple[int, str]] = []
    energy: list[tuple[int, str]] = []
    good = ("solar", "pv", "inverter", "omvormer", "yield", "opbrengst", "production", "productie", "string")
    bad = ("grid", "import", "export", "consumption", "verbruik", "load", "house", "p1", "meter") + _BATTERY
    for e in _sensor_entries(hass):
        if e.platform == HOMEWIZARD or e.entity_id in exclude:
            continue
        dc, unit, sc = _attrs(hass, e)
        text = _text(e)
        if dc == "power" and unit in FACTORS["power"]:
            score = 3 * _count(text, good + ("input", "dc", "current", "active")) - 5 * _count(text, bad + _BAD_PERIOD)
            if hybrid:
                score += 4 * _count(text, ("input", "dc", "pv")) - 6 * _count(text, ("active", "output", "ac_power", "uitgang"))
            power.append((score, e.entity_id))
        elif dc == "energy" and unit in FACTORS["energy"] and sc in ("total_increasing", "total", None):
            score = 3 * _count(text, good + ("lifetime", "total", "totaal")) - 5 * _count(text, bad + _BAD_PERIOD)
            if hybrid:
                score += 3 * _count(text, ("pv", "input", "dc")) - 4 * _count(text, ("accumulated", "output"))
            energy.append((score, e.entity_id))
    out: dict[str, str] = {}
    if (p := _best(power)) is not None:
        out["power"] = p
    if (en := _best(energy)) is not None:
        out["energy"] = en
    return out


def suggest_battery(hass: HomeAssistant) -> dict[str, str]:
    """Beste gok voor batterijvermogen, laadniveau en de tellers voor geladen/ontladen."""
    power: list[tuple[int, str]] = []
    soc: list[tuple[int, str]] = []
    charge: list[tuple[int, str]] = []
    discharge: list[tuple[int, str]] = []
    for e in _sensor_entries(hass):
        dc, unit, sc = _attrs(hass, e)
        text = _text(e)
        if not _count(text, _BATTERY):
            continue
        if dc == "power" and unit in FACTORS["power"]:
            power.append((5 + 2 * _count(text, ("charge", "discharge", "laad", "power")) - 4 * _count(text, ("max", "limit", "rated")), e.entity_id))
        elif dc == "battery" and unit == "%":
            soc.append((5 + 3 * _count(text, ("soc", "state_of_charge", "state of capacity", "level", "capacity")) - 6 * _count(text, ("health", "soh")), e.entity_id))
        elif dc == "energy" and unit in FACTORS["energy"] and sc in ("total_increasing", "total", None):
            if _count(text, _BAD_PERIOD):
                continue
            if _count(text, ("discharge", "ontla")):
                discharge.append((5 + _count(text, ("total", "totaal", "lifetime")), e.entity_id))
            elif _count(text, ("charge", "laad", "geladen")):
                charge.append((5 + _count(text, ("total", "totaal", "lifetime")), e.entity_id))
    out: dict[str, str] = {}
    for key, cands in (("power", power), ("soc", soc), ("charge", charge), ("discharge", discharge)):
        if (best := _best(cands)) is not None:
            out[key] = best
    if ("charge" in out) != ("discharge" in out):  # alleen beide of geen
        out.pop("charge", None)
        out.pop("discharge", None)
    return out


def suggest_house(hass: HomeAssistant) -> str | None:
    """Huisverbruik-sensor van de omvormer (bijv. 'load power'), als die er is."""
    cands: list[tuple[int, str]] = []
    for e in _sensor_entries(hass):
        if e.platform == HOMEWIZARD:
            continue
        dc, unit, _ = _attrs(hass, e)
        text = _text(e)
        if dc == "power" and unit in FACTORS["power"]:
            score = 4 * _count(text, ("house", "load", "consumption", "huis", "verbruik")) - 5 * _count(text, _BATTERY + ("pv", "solar", "grid", "input"))
            cands.append((score, e.entity_id))
    return _best(cands)
