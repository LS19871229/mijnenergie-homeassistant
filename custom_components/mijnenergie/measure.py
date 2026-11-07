"""Sensorkeuze (configuratie), eenheden omrekenen en metingen opbouwen.

Bewust zonder Home Assistant-imports, zodat dit los te testen is.
Het portaal verwacht: vermogen in W, energie in kWh, gas in m³, laadniveau in %.

Configuratie (zelfde formaat als het portaal, zie lib/sensorconfig.php):
{
  "installation": "none|inverter|hybrid|inverter_battery|multi",
  "p1":      {"power": "sensor.x", "import": "...", "export": "...", "gas": "", "invert": false},
  "pv":      [{"label": "Voor", "power": "...", "energy": "..."}],
  "battery": [{"label": "", "power": "...", "soc": "...", "charge": "", "discharge": "", "invert": false, "capacity": 10.0}],
  "house":   {"power": ""}
}
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from .const import (
    CONF_BATTERY_INVERT,
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_GAS,
    CONF_P1_EXPORT,
    CONF_P1_IMPORT,
    CONF_P1_POWER,
    CONF_PV_ENERGY,
    CONF_PV_POWER,
    INST_INVERTER,
    INST_INVERTER_BATTERY,
    INST_NONE,
    INSTALLATIONS,
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
ENTITY_RE = re.compile(r"^sensor\.[a-z0-9_]{1,250}$")
MAX_PV = 6
MAX_BATTERY = 4

StateGetter = Callable[[str], "tuple[Any, str | None] | None"]


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


# ---------------------------------------------------------------- configuratie

def _entity(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if ENTITY_RE.match(text) else ""


def empty_config() -> dict[str, Any]:
    return {"installation": INST_INVERTER, "p1": {"power": "", "import": "", "export": "", "gas": "", "invert": False},
            "pv": [], "battery": [], "house": {"power": ""}}


def normalize_config(raw: Any) -> dict[str, Any]:
    """Schone configuratie, ongeacht wat er binnenkomt (van het portaal of uit oude opties)."""
    cfg = empty_config()
    if not isinstance(raw, Mapping):
        return cfg
    inst = raw.get("installation")
    cfg["installation"] = inst if inst in INSTALLATIONS else INST_INVERTER
    p1 = raw.get("p1") if isinstance(raw.get("p1"), Mapping) else {}
    for key in ("power", "import", "export", "gas"):
        cfg["p1"][key] = _entity(p1.get(key))
    cfg["p1"]["invert"] = bool(p1.get("invert")) and p1.get("invert") not in ("0", "false")
    for src in (raw.get("pv") if isinstance(raw.get("pv"), list) else [])[:MAX_PV]:
        if not isinstance(src, Mapping):
            continue
        row = {"label": str(src.get("label") or "")[:40], "power": _entity(src.get("power")), "energy": _entity(src.get("energy"))}
        if row["power"] or row["energy"]:
            cfg["pv"].append(row)
    for src in (raw.get("battery") if isinstance(raw.get("battery"), list) else [])[:MAX_BATTERY]:
        if not isinstance(src, Mapping):
            continue
        cap = to_number(src.get("capacity"))
        row = {
            "label": str(src.get("label") or "")[:40],
            "power": _entity(src.get("power")), "soc": _entity(src.get("soc")),
            "charge": _entity(src.get("charge")), "discharge": _entity(src.get("discharge")),
            "invert": bool(src.get("invert")) and src.get("invert") not in ("0", "false"),
            "capacity": round(cap, 2) if cap is not None and 0 < cap < 1000 else None,
        }
        if row["power"] or row["soc"] or row["charge"] or row["discharge"]:
            cfg["battery"].append(row)
    house = raw.get("house") if isinstance(raw.get("house"), Mapping) else {}
    cfg["house"]["power"] = _entity(house.get("power"))
    if cfg["installation"] == INST_NONE:
        cfg["pv"], cfg["battery"], cfg["house"]["power"] = [], [], ""
    elif cfg["installation"] == INST_INVERTER:
        cfg["battery"] = []
    # Onderdelen die deze versie nog niet kent (bijv. laadpaal of warmtepomp uit het portaal) blijven bewaard,
    # zodat ze worden meegelezen en niet verloren gaan bij terugsturen. Het portaal rekent ermee.
    for key, value in raw.items():
        if key not in cfg and isinstance(key, str) and _KEY_RE.match(key):
            kept = _keep_unknown(value)
            if kept is not None:
                cfg[key] = kept
    return cfg


_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")


def _keep_unknown(value: Any, depth: int = 0) -> Any:
    """Onbekend deel van de configuratie veilig overnemen (alleen JSON-waarden, beperkt in grootte)."""
    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:250]
    if isinstance(value, list):
        return [v for v in (_keep_unknown(x, depth + 1) for x in value[:20]) if v is not None]
    if isinstance(value, Mapping):
        out = {}
        for k, v in list(value.items())[:30]:
            if isinstance(k, str) and _KEY_RE.match(k):
                kept = _keep_unknown(v, depth + 1)
                if kept is not None:
                    out[k] = kept
        return out
    return None


def config_from_v1(options: Mapping[str, Any], home_mode: str = "pv") -> dict[str, Any]:
    """Platte opties van integratie v1.0 omzetten naar de configuratie van v1.1."""
    has_pv = bool(options.get(CONF_PV_POWER) or options.get(CONF_PV_ENERGY))
    has_bat = bool(options.get(CONF_BATTERY_POWER) or options.get(CONF_BATTERY_SOC))
    if not has_pv:
        inst = INST_NONE
    elif has_bat:
        inst = INST_INVERTER_BATTERY  # v1.0 wist niet of de batterij hybride was; dit rekent hetzelfde
    else:
        inst = INST_INVERTER
    raw = {
        "installation": inst,
        "p1": {"power": options.get(CONF_P1_POWER), "import": options.get(CONF_P1_IMPORT),
               "export": options.get(CONF_P1_EXPORT), "gas": options.get(CONF_GAS)},
        "pv": [{"label": "", "power": options.get(CONF_PV_POWER), "energy": options.get(CONF_PV_ENERGY)}] if has_pv else [],
        "battery": [{"label": "", "power": options.get(CONF_BATTERY_POWER), "soc": options.get(CONF_BATTERY_SOC),
                     "invert": bool(options.get(CONF_BATTERY_INVERT))}] if has_bat else [],
    }
    return normalize_config(raw)


def config_entities(cfg: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Alle sensoren in de configuratie als (veld, entity_id), bijv. ("pv.0.power", "sensor.x").

    Zoekt de hele configuratie door: velden die het portaal later toevoegt, worden zo vanzelf meegelezen.
    """
    out: list[tuple[str, str]] = []
    if isinstance(cfg, Mapping):
        for key, value in cfg.items():
            out += config_entities(value, f"{prefix}{key}.")
    elif isinstance(cfg, list):
        for i, value in enumerate(cfg):
            out += config_entities(value, f"{prefix}{i}.")
    elif isinstance(cfg, str) and ENTITY_RE.match(cfg):
        out.append((prefix[:-1], cfg))
    return out


def build_from_raw(cfg: Mapping[str, Any], m: Mapping[str, Any]) -> dict[str, Any]:
    """Meting uit een ruwe meting {"ts", "raw"} (zoals het portaal rekent; voor tests en diagnose)."""
    raw = m.get("raw") or {}

    def get_state(entity_id: str) -> tuple[Any, str | None] | None:
        v = raw.get(entity_id)
        return None if v is None else (v[0], v[1])

    return build_measurement(datetime.fromisoformat(m["ts"]), cfg, get_state)


def collect_raw(cfg: Mapping[str, Any], get_state: StateGetter) -> dict[str, list[Any]]:
    """Ruwe waarden [toestand, eenheid] van alle gekozen sensoren. Het portaal rekent hiermee (v1.3+)."""
    raw: dict[str, list[Any]] = {}
    for _, entity_id in config_entities(cfg):
        got = get_state(entity_id)
        raw[entity_id] = [None, None] if got is None else [None if got[0] is None else str(got[0]), got[1]]
    return raw


def rename_entity(cfg: Mapping[str, Any], old: str, new: str) -> dict[str, Any] | None:
    """Nieuwe configuratie met old -> new vervangen, of None als old er niet in staat."""
    if not any(eid == old for _, eid in config_entities(cfg)):
        return None
    out = normalize_config(cfg)
    for key, value in out["p1"].items():
        if key != "invert" and value == old:
            out["p1"][key] = new
    for group in ("pv", "battery"):
        for src in out[group]:
            for key, value in src.items():
                if value == old:
                    src[key] = new
    if out["house"]["power"] == old:
        out["house"]["power"] = new
    return out


def config_meters(cfg: Mapping[str, Any]) -> list[str]:
    """Gekozen meters, zoals het portaal ze toont."""
    meters = []
    if cfg["p1"].get("power"):
        meters.append("p1")
    if cfg["p1"].get("gas"):
        meters.append("gas")
    if cfg["pv"]:
        meters.append("pv")
    if cfg["battery"]:
        meters.append("battery")
    if cfg["house"].get("power"):
        meters.append("house")
    return meters


# ---------------------------------------------------------------- meting

def build_measurement(when: datetime, cfg: Mapping[str, Any], get_state: StateGetter) -> dict[str, Any]:
    """Eén meting in het formaat van api/ingest.php.

    get_state(entity_id) geeft (toestand, eenheid) of None als de sensor niet bestaat.
    Een som (meerdere omvormers of batterijen) wordt alleen verstuurd als alle delen bekend zijn;
    anders null. Een halve som zou een verkeerde sprong in de tellerstanden geven.
    """

    def read(entity_id: str, kind: str) -> float | None:
        if not entity_id:
            return None
        got = get_state(entity_id)
        if got is None:
            return None
        return convert(got[0], got[1], kind)

    def total(values: list[float | None]) -> float | None:
        if not values or any(v is None for v in values):
            return None
        return float(sum(v for v in values if v is not None))

    def rnd(value: float | None, digits: int) -> float | None:
        return None if value is None else round(value, digits)

    p1 = cfg["p1"]
    net = read(p1.get("power", ""), "power")
    if net is not None and p1.get("invert"):
        net = -net  # sommige meters/omvormers geven afname als negatief getal
    pv_parts = [read(src["power"], "power") for src in cfg["pv"] if src.get("power")]
    pv_energy = [read(src["energy"], "energy") for src in cfg["pv"] if src.get("energy")]

    bat_power: list[float | None] = []
    soc_values: list[tuple[float | None, float]] = []
    charge: list[float | None] = []
    discharge: list[float | None] = []
    for src in cfg["battery"]:
        if src.get("power"):
            w = read(src["power"], "power")
            bat_power.append(None if w is None else (-w if src.get("invert") else w))
        if src.get("soc"):
            soc_values.append((read(src["soc"], "percent"), float(src.get("capacity") or 1.0)))
        if src.get("charge") and src.get("discharge"):
            charge.append(read(src["charge"], "energy"))
            discharge.append(read(src["discharge"], "energy"))
    soc: float | None = None
    if soc_values and all(v is not None for v, _ in soc_values):
        weight = sum(c for _, c in soc_values)
        soc = sum((v or 0.0) * c for v, c in soc_values) / weight if weight else None
    complete_counters = len(charge) == len(cfg["battery"]) and bool(charge)

    measurement: dict[str, Any] = {
        "ts": when.isoformat(timespec="seconds"),
        "power": {
            "net_w": rnd(net, 1),
            "pv_w": rnd(total(pv_parts), 1),
            "battery_w": rnd(total(bat_power), 1),
            "house_w": rnd(read(cfg["house"].get("power", ""), "power"), 1),
        },
        "energy": {
            "import_kwh": rnd(read(p1.get("import", ""), "energy"), 4),
            "export_kwh": rnd(read(p1.get("export", ""), "energy"), 4),
            "pv_kwh": rnd(total(pv_energy), 4),
            "gas_m3": rnd(read(p1.get("gas", ""), "gas"), 4),
            "battery_charge_kwh": rnd(total(charge), 4) if complete_counters else None,
            "battery_discharge_kwh": rnd(total(discharge), 4) if complete_counters else None,
        },
        "battery_soc": rnd(soc, 1),
    }
    if len(pv_parts) > 1:
        measurement["power"]["pv_parts"] = [rnd(v, 0) for v in pv_parts]
    return measurement

