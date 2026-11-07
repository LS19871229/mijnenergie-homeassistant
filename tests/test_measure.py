"""Configuratie, omrekenen en metingen opbouwen (zonder Home Assistant)."""

from datetime import UTC, datetime

from pytest import approx

from custom_components.mijnenergie.measure import (
    build_measurement,
    config_entities,
    config_from_v1,
    config_meters,
    convert,
    normalize_config,
    rename_entity,
    to_number,
    unit_supported,
)

WHEN = datetime(2026, 11, 6, 13, 42, tzinfo=UTC)


def getter(states: dict[str, tuple]):
    return lambda entity_id: states.get(entity_id)


def test_to_number_and_convert() -> None:
    assert to_number("12,5") == 12.5
    for bad in (None, "unknown", "unavailable", "", "abc", "nan", "inf", True):
        assert to_number(bad) is None
    assert convert("1.24", "kW", "power") == 1240
    assert convert("9876300", "Wh", "energy") == approx(9876.3)
    assert convert("3.6", "GJ", "energy") == approx(1000)
    assert convert("10", "mph", "power") is None
    assert unit_supported("energy", "kWh") and not unit_supported("energy", "W")


def test_normalize_config_is_robust() -> None:
    assert normalize_config(None)["installation"] == "inverter"
    cfg = normalize_config({
        "installation": "hybrid",
        "p1": {"power": "sensor.p 1", "import": "light.niet_goed", "export": "sensor.x"},
        "pv": [{"power": "sensor.a", "energy": "sensor.b"}, {"label": "leeg"}, "rommel"],
        "battery": [{"power": "sensor.bp", "soc": "sensor.bs", "invert": "1", "capacity": "10,5"}],
        "house": "geen dict",
    })
    assert cfg["p1"]["power"] == ""  # spatie binnen de naam: ongeldig
    assert cfg["p1"]["import"] == ""  # geen sensor
    assert cfg["pv"] == [{"label": "", "power": "sensor.a", "energy": "sensor.b"}]
    assert cfg["battery"][0]["invert"] is True and cfg["battery"][0]["capacity"] == 10.5
    assert normalize_config({"installation": "inverter", "battery": [{"power": "sensor.bp"}]})["battery"] == []
    assert normalize_config({"installation": "none", "pv": [{"power": "sensor.a"}]})["pv"] == []


def test_hybrid_measurement_and_house_balance() -> None:
    cfg = normalize_config({
        "installation": "hybrid",
        "p1": {"power": "sensor.net", "import": "sensor.imp", "export": "sensor.exp"},
        "pv": [{"power": "sensor.pv_in", "energy": "sensor.pv_tot"}],
        "battery": [{"power": "sensor.bat", "soc": "sensor.soc", "charge": "sensor.c", "discharge": "sensor.d", "capacity": 10}],
        "house": {"power": "sensor.load"},
    })
    m = build_measurement(WHEN, cfg, getter({
        "sensor.net": ("-720", "W"), "sensor.imp": ("4123.5", "kWh"), "sensor.exp": ("2210", "kWh"),
        "sensor.pv_in": ("3.2", "kW"), "sensor.pv_tot": ("9876", "kWh"),
        "sensor.bat": ("1500", "W"), "sensor.soc": ("55", "%"), "sensor.c": ("1802", "kWh"), "sensor.d": ("1655", "kWh"),
        "sensor.load": ("1010", "W"),
    }))
    p = m["power"]
    assert p == {"net_w": -720.0, "pv_w": 3200.0, "battery_w": 1500.0, "house_w": 1010.0}
    assert p["net_w"] + p["pv_w"] - p["battery_w"] == approx(980)  # huis berekend
    assert m["energy"]["battery_charge_kwh"] == 1802 and m["energy"]["battery_discharge_kwh"] == 1655
    assert m["battery_soc"] == 55


def test_multiple_inverters_and_batteries_are_summed() -> None:
    cfg = normalize_config({
        "installation": "multi",
        "p1": {"power": "sensor.net"},
        "pv": [{"label": "Voor", "power": "sensor.p1w", "energy": "sensor.p1e"}, {"label": "Garage", "power": "sensor.p2w", "energy": "sensor.p2e"}],
        "battery": [{"power": "sensor.b1", "soc": "sensor.s1", "capacity": 10},
                    {"power": "sensor.b2", "soc": "sensor.s2", "capacity": 5, "invert": True}],
    })
    states = {"sensor.net": ("0", "W"), "sensor.p1w": ("3200", "W"), "sensor.p1e": ("1000", "kWh"),
              "sensor.p2w": ("0.9", "kW"), "sensor.p2e": ("500000", "Wh"),
              "sensor.b1": ("1000", "W"), "sensor.s1": ("60", "%"), "sensor.b2": ("200", "W"), "sensor.s2": ("30", "%")}
    m = build_measurement(WHEN, cfg, getter(states))
    assert m["power"]["pv_w"] == 4100.0
    assert m["power"]["pv_parts"] == [3200.0, 900.0]
    assert m["energy"]["pv_kwh"] == 1500.0
    assert m["power"]["battery_w"] == 800.0  # 1000 − 200 (omgedraaid)
    assert m["battery_soc"] == 50.0  # gewogen naar capaciteit: (60*10 + 30*5) / 15
    assert m["energy"]["battery_charge_kwh"] is None  # geen tellers gekozen


def test_partial_sums_become_null() -> None:
    """Valt één omvormer weg, dan geen halve som (dat zou een sprong in de meterstand geven)."""
    cfg = normalize_config({"installation": "multi", "p1": {"power": "sensor.net"},
                            "pv": [{"power": "sensor.a", "energy": "sensor.ae"}, {"power": "sensor.b", "energy": "sensor.be"}]})
    m = build_measurement(WHEN, cfg, getter({"sensor.net": ("1", "W"), "sensor.a": ("100", "W"), "sensor.ae": ("5", "kWh"),
                                             "sensor.b": ("unavailable", "W")}))
    assert m["power"]["pv_w"] is None
    assert m["energy"]["pv_kwh"] is None
    assert m["power"]["net_w"] == 1.0


def test_v1_migration_and_helpers() -> None:
    v1 = {"p1_power": "sensor.p", "p1_import": "sensor.i", "p1_export": "sensor.e", "gas": "",
          "pv_power": "sensor.pv", "pv_energy": "sensor.pve", "battery_power": "sensor.b", "battery_soc": "sensor.s", "battery_invert": True}
    cfg = config_from_v1(v1)
    assert cfg["installation"] == "inverter_battery"
    assert cfg["battery"][0]["invert"] is True
    assert config_meters(cfg) == ["p1", "pv", "battery"]
    assert config_from_v1({"p1_power": "sensor.p"})["installation"] == "none"
    assert ("pv.0.power", "sensor.pv") in config_entities(cfg)
    renamed = rename_entity(cfg, "sensor.pv", "sensor.omvormer")
    assert renamed["pv"][0]["power"] == "sensor.omvormer"
    assert rename_entity(cfg, "sensor.bestaat_niet", "sensor.x") is None
