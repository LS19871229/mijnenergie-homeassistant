"""Constanten voor de Mijn Energie-integratie."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "mijnenergie"
DEFAULT_URL: Final = "https://eltrading.nl/mijnenergie"

# Gegevens in de config entry (data)
CONF_URL: Final = "url"
CONF_TOKEN: Final = "token"
CONF_CODE: Final = "code"
CONF_HOME_ID: Final = "home_id"
CONF_HOME_NAME: Final = "home_name"
CONF_HOME_MODE: Final = "home_mode"  # "pv" of "meetfase"

# Gekozen sensoren (config entry options)
CONF_P1_POWER: Final = "p1_power"
CONF_P1_IMPORT: Final = "p1_import"
CONF_P1_EXPORT: Final = "p1_export"
CONF_GAS: Final = "gas"
CONF_PV_POWER: Final = "pv_power"
CONF_PV_ENERGY: Final = "pv_energy"
CONF_BATTERY_POWER: Final = "battery_power"
CONF_BATTERY_SOC: Final = "battery_soc"
CONF_BATTERY_INVERT: Final = "battery_invert"

# Soort meting per sensor, voor het controleren en omrekenen van eenheden
SENSOR_KINDS: Final[dict[str, str]] = {
    CONF_P1_POWER: "power",
    CONF_P1_IMPORT: "energy",
    CONF_P1_EXPORT: "energy",
    CONF_GAS: "gas",
    CONF_PV_POWER: "power",
    CONF_PV_ENERGY: "energy",
    CONF_BATTERY_POWER: "power",
    CONF_BATTERY_SOC: "percent",
}

# Welke meter (zoals het portaal ze toont) hoort bij welke sensoren
METERS: Final[dict[str, tuple[str, ...]]] = {
    "p1": (CONF_P1_POWER, CONF_P1_IMPORT, CONF_P1_EXPORT),
    "gas": (CONF_GAS,),
    "pv": (CONF_PV_POWER, CONF_PV_ENERGY),
    "battery": (CONF_BATTERY_POWER, CONF_BATTERY_SOC),
}

MODE_PV: Final = "pv"
MODE_MEETFASE: Final = "meetfase"

SEND_INTERVAL_SECONDS: Final = 60
QUEUE_MAX: Final = 7 * 24 * 60  # 7 dagen aan minuutmetingen
BATCH_SIZE: Final = 500
OFFLINE_ISSUE_AFTER_SECONDS: Final = 30 * 60
MISSING_ENTITY_GRACE_SECONDS: Final = 10 * 60
STORAGE_VERSION: Final = 1
SAVE_DELAY_SECONDS: Final = 300  # wachtrij niet elke minuut naar de opslag schrijven

STATUS_STARTING: Final = "starting"
STATUS_CONNECTED: Final = "connected"
STATUS_OFFLINE: Final = "offline"
STATUS_AUTH_FAILED: Final = "auth_failed"
STATUSES: Final = [STATUS_STARTING, STATUS_CONNECTED, STATUS_OFFLINE, STATUS_AUTH_FAILED]


def signal_update(entry_id: str) -> str:
    """Dispatcher-signaal waarmee de statussensoren worden bijgewerkt."""
    return f"{DOMAIN}_update_{entry_id}"
