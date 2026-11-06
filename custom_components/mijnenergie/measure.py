"""Metingen opbouwen en eenheden omrekenen.

Bewust zonder Home Assistant-imports, zodat dit los te testen is.
Het portaal verwacht: vermogen in W, energie in kWh, gas in m³, laadniveau in %.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from .const import (
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_GAS,
    CONF_P1_EXPORT,
    CONF_P1_IMPORT,
    CONF_P1_POWER,
    CONF_PV_ENERGY,
    CONF_PV_POWER,
    SENSOR_KINDS,
)

# Factor naar de eenheid die het portaal verwacht
FACTORS: dict[str, dict[str | None, float]] = {
    "power": {None: 1.0, "W": 1.0, "kW": 1000.0, "MW": 1_000_000.0, "mW": 0.001},
    "energy": {None: 1.0, "Wh": 0.001, "kWh": 1.0, "MWh": 1000.0, "GWh": 1_000_000.0,
               "MJ": 1 / 3.6, "GJ": 1000 / 3.6},
    "gas": {None: 1.0, "m³": 1.0, "m3": 1.0, "L": 0.001, "ft³": 0.0283168466, "CCF": 2.83168466},
    "percent": {None: 1.0, "%": 1.0},
}

UNAVAILABLE = {"unknown", "unavailable", "none", "", "nan"}


def unit_supported(kind: str, unit: str | None) -> bool:
    """Kunnen we deze eenheid omrekenen?"""
    return unit in FACTORS.get(kind, {})


def to_number(state: Any) -> float | None:
    """Toestand van een sensor als getal, of None als die onbekend is."""
    if state is None or isinstance(state, bool):
        return None
    if isinstance(state, (int, float)):
        value = float(state)
    else:
        text = str(state).strip()
        if text.lower() in UNAVAILABLE:
            return None
        try:
            value = float(text.replace(",", "."))
        except ValueError:
            return None
    return value if math.isfinite(value) else None


def convert(state: Any, unit: str | None, kind: str) -> float | None:
    """Toestand omrekenen naar de eenheid van het portaal (None bij onbekende eenheid)."""
    value = to_number(state)
    factor = FACTORS.get(kind, {}).get(unit)
    if value is None or factor is None:
        return None
    return value * factor


def build_measurement(
    when: datetime,
    readings: dict[str, tuple[Any, str | None]],
    battery_invert: bool = False,
) -> dict[str, Any]:
    """Eén meting in het formaat van api/ingest.php.

    readings: sleutel (zoals CONF_P1_POWER) -> (toestand, eenheid). Ontbrekende sleutels worden null.
    """

    def value(key: str, digits: int) -> float | None:
        if key not in readings:
            return None
        state, unit = readings[key]
        result = convert(state, unit, SENSOR_KINDS[key])
        return None if result is None else round(result, digits)

    battery_w = value(CONF_BATTERY_POWER, 1)
    if battery_w is not None and battery_invert:
        battery_w = -battery_w

    return {
        "ts": when.isoformat(timespec="seconds"),
        "power": {
            "net_w": value(CONF_P1_POWER, 1),
            "pv_w": value(CONF_PV_POWER, 1),
            "battery_w": battery_w,
        },
        "energy": {
            "import_kwh": value(CONF_P1_IMPORT, 4),
            "export_kwh": value(CONF_P1_EXPORT, 4),
            "pv_kwh": value(CONF_PV_ENERGY, 4),
            "gas_m3": value(CONF_GAS, 4),
        },
        "battery_soc": value(CONF_BATTERY_SOC, 1),
    }
