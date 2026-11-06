"""Omrekenen en opbouwen van metingen (zonder Home Assistant)."""

from datetime import datetime, timezone

from pytest import approx

from custom_components.mijnenergie.const import (
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_GAS,
    CONF_P1_EXPORT,
    CONF_P1_IMPORT,
    CONF_P1_POWER,
    CONF_PV_ENERGY,
    CONF_PV_POWER,
)
from custom_components.mijnenergie.measure import build_measurement, convert, to_number, unit_supported

WHEN = datetime(2026, 11, 6, 13, 42, tzinfo=timezone.utc)


def test_to_number() -> None:
    assert to_number("12.5") == 12.5
    assert to_number("12,5") == 12.5
    assert to_number(7) == 7.0
    for bad in (None, "unknown", "unavailable", "", "abc", "nan", "inf", True):
        assert to_number(bad) is None


def test_convert_units() -> None:
    assert convert("1.24", "kW", "power") == 1240
    assert convert("9876300", "Wh", "energy") == approx(9876.3)
    assert convert("9.8763", "MWh", "energy") == approx(9876.3)
    assert convert("3.6", "GJ", "energy") == approx(1000)
    assert convert("100", "W", "power") == 100
    assert convert("5", "ft³", "gas") == approx(5 * 0.0283168466)
    assert convert("10", "mph", "power") is None  # onbekende eenheid
    assert convert("10", None, "power") == 10  # geen eenheid: aannemen dat het W is
    assert unit_supported("energy", "kWh")
    assert not unit_supported("energy", "W")


def test_build_measurement_full() -> None:
    m = build_measurement(
        WHEN,
        {
            CONF_P1_POWER: ("-720", "W"),
            CONF_P1_IMPORT: ("4123.512", "kWh"),
            CONF_P1_EXPORT: ("2210.044", "kWh"),
            CONF_GAS: ("1532.118", "m³"),
            CONF_PV_POWER: ("1.24", "kW"),
            CONF_PV_ENERGY: ("9876300", "Wh"),
            CONF_BATTERY_POWER: ("500", "W"),
            CONF_BATTERY_SOC: ("55", "%"),
        },
        battery_invert=True,
    )
    assert m["ts"] == "2026-11-06T13:42:00+00:00"
    assert m["power"] == {"net_w": -720.0, "pv_w": 1240.0, "battery_w": -500.0}
    assert m["energy"] == {"import_kwh": 4123.512, "export_kwh": 2210.044, "pv_kwh": 9876.3, "gas_m3": 1532.118}
    assert m["battery_soc"] == 55.0


def test_build_measurement_missing_and_unavailable() -> None:
    m = build_measurement(WHEN, {CONF_P1_POWER: ("unavailable", "W"), CONF_P1_IMPORT: ("1", "kWh")})
    assert m["power"]["net_w"] is None
    assert m["power"]["pv_w"] is None
    assert m["energy"]["import_kwh"] == 1.0
    assert m["energy"]["gas_m3"] is None
    assert m["battery_soc"] is None
