"""Elke minuut meten, in de wachtrij zetten en naar het portaal sturen.

Daarnaast:
- sensorkeuze synchroon houden met het portaal (het portaal is leidend, wijzigingen in HA gaan terug);
- elke 10 minuten de lijst met bruikbare sensoren naar het portaal sturen (voor de keuzelijsten in het beheer).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_FRIENDLY_NAME, ATTR_UNIT_OF_MEASUREMENT
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_change, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import CannotConnect, InvalidAuth, MijnEnergieApi
from .const import (
    AUTO_UPDATE_HOUR,
    BATCH_SIZE,
    CONF_AUTO_UPDATE,
    CONF_CONFIG,
    CONF_CONFIG_DIRTY,
    CONF_CONFIG_REV,
    DOMAIN,
    INVENTORY_INTERVAL_SECONDS,
    MISSING_ENTITY_GRACE_SECONDS,
    OFFLINE_ISSUE_AFTER_SECONDS,
    QUEUE_MAX,
    REPO_SLUG,
    SAVE_DELAY_SECONDS,
    STATUS_AUTH_FAILED,
    STATUS_CONNECTED,
    STATUS_OFFLINE,
    STATUS_STARTING,
    STORAGE_VERSION,
    signal_update,
)
from .measure import FACTORS, build_measurement, collect_raw, config_entities, config_meters, normalize_config

_LOGGER = logging.getLogger(__name__)

SUPPORTED_UNITS = {u for units in FACTORS.values() for u in units if u}


def queue_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Opslag van de wachtrij (blijft bewaard na een herstart)."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.queue")


def entry_config(entry: ConfigEntry) -> dict[str, Any]:
    return normalize_config(entry.options.get(CONF_CONFIG))


class MijnEnergieSender:
    """Verzamelt metingen en verstuurt ze; houdt de status bij voor de sensoren."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: MijnEnergieApi, version: str) -> None:
        self.hass = hass
        self.entry = entry
        self.api = api
        self.version = version
        self.queue: list[dict[str, Any]] = []
        self.status: str = STATUS_STARTING
        self.last_sent: datetime | None = None
        self.last_error: str | None = None
        self.offline_since: datetime | None = None
        self.last_measurement: dict[str, Any] | None = None
        self.last_inventory: datetime | None = None
        self.last_computed: dict[str, Any] | None = None
        self.last_update: str | None = None
        self._store = queue_store(hass, entry.entry_id)
        self._lock = asyncio.Lock()
        self._unsub: list[CALLBACK_TYPE] = []
        self._missing_since: dict[str, datetime] = {}
        self._reauth_started = False

    # ---------- levensloop ----------

    async def async_start(self) -> None:
        """Wachtrij laden en de timers starten."""
        stored = await self._store.async_load()
        if isinstance(stored, dict) and isinstance(stored.get("queue"), list):
            self.queue = [m for m in stored["queue"] if isinstance(m, dict)][-QUEUE_MAX:]
        self._unsub.append(async_track_time_change(self.hass, self._tick, second=0))
        # Meteen na het starten een eerste meting, zodat je niet een minuut hoeft te wachten
        self._unsub.append(async_call_later(self.hass, 5, self._tick))
        # Sensorlijst: kort na het starten (als alle integraties geladen zijn) en daarna elke 10 minuten
        self._unsub.append(async_call_later(self.hass, 30, self._inventory_tick))
        self._unsub.append(async_track_time_interval(self.hass, self._inventory_tick, timedelta(seconds=INVENTORY_INTERVAL_SECONDS)))
        # Automatisch bijwerken: elke nacht om 03:xx (minuut verschilt per HA Green, zodat niet alles tegelijk herstart)
        minute = sum(map(ord, self.entry.entry_id)) % 30
        self._unsub.append(async_track_time_change(self.hass, self._auto_update_tick, hour=AUTO_UPDATE_HOUR, minute=minute, second=30))

    async def async_stop(self) -> None:
        """Timers stoppen en de wachtrij direct opslaan."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        await self._store.async_save(self._data_to_save())

    def _data_to_save(self) -> dict[str, Any]:
        return {"queue": self.queue}

    # ---------- configuratie ----------

    @property
    def config(self) -> dict[str, Any]:
        return entry_config(self.entry)

    @property
    def config_rev(self) -> int:
        return int(self.entry.options.get(CONF_CONFIG_REV) or 0)

    @property
    def meters(self) -> list[str]:
        return config_meters(self.config)

    def _set_options(self, **changes: Any) -> None:
        self.hass.config_entries.async_update_entry(self.entry, options={**self.entry.options, **changes})

    def _apply_portal_config(self, config: dict[str, Any], rev: int, by: str | None) -> None:
        """Nieuwe sensorkeuze uit het portaal overnemen."""
        cfg = normalize_config(config)
        self._set_options(**{CONF_CONFIG: cfg, CONF_CONFIG_REV: rev, CONF_CONFIG_DIRTY: False})
        self._missing_since.clear()
        _LOGGER.info("Mijn Energie: sensorkeuze versie %s overgenomen uit het portaal", rev)
        self._logbook(f"Sensorkeuze bijgewerkt vanuit het portaal (versie {rev}{', door ' + by if by else ''}).")

    def _logbook(self, message: str) -> None:
        if self.hass.services.has_service("logbook", "log"):
            self.hass.async_create_task(
                self.hass.services.async_call("logbook", "log", {"name": "Mijn Energie", "message": message, "domain": DOMAIN})
            )

    async def _push_config_if_needed(self, server_rev: int | None) -> None:
        """Lokale wijziging (of geen keuze in het portaal) naar het portaal sturen."""
        dirty = bool(self.entry.options.get(CONF_CONFIG_DIRTY))
        if not dirty and not (server_rev == 0 and config_entities(self.config)):
            return
        rev = await self.api.push_config(self.config)
        self._set_options(**{CONF_CONFIG_REV: rev, CONF_CONFIG_DIRTY: False})
        _LOGGER.info("Mijn Energie: sensorkeuze naar het portaal gestuurd (versie %s)", rev)
        self._logbook(f"Sensorkeuze gewijzigd in Home Assistant en naar het portaal gestuurd (versie {rev}).")

    # ---------- meten ----------

    def measure(self, when: datetime) -> dict[str, Any]:
        """Lees de gekozen sensoren. Het portaal rekent met de ruwe waarden; lokaal rekenen we alleen voor de diagnose."""
        cfg = self.config
        for field, entity_id in config_entities(cfg):
            self._track_missing(field, entity_id, self.hass.states.get(entity_id) is None, when)

        def get_state(entity_id: str) -> tuple[Any, str | None] | None:
            st = self.hass.states.get(entity_id)
            return None if st is None else (st.state, st.attributes.get(ATTR_UNIT_OF_MEASUREMENT))

        self.last_computed = build_measurement(when, cfg, get_state)
        return {"ts": when.isoformat(timespec="seconds"), "raw": collect_raw(cfg, get_state)}

    def _track_missing(self, field: str, entity_id: str, missing: bool, when: datetime) -> None:
        """Melding als een gekozen sensor langer dan 10 minuten niet bestaat."""
        issue_id = f"entity_missing_{self.entry.entry_id}_{field}"
        if not missing:
            if self._missing_since.pop(field, None) is not None:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        since = self._missing_since.setdefault(field, when)
        if (when - since).total_seconds() >= MISSING_ENTITY_GRACE_SECONDS:
            ir.async_create_issue(
                self.hass, DOMAIN, issue_id,
                is_fixable=False, is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="entity_missing",
                translation_placeholders={"entity": entity_id, "home": self.entry.title},
            )

    # ---------- elke minuut ----------

    @callback
    def _tick(self, now: datetime | None = None) -> None:
        self.entry.async_create_background_task(self.hass, self.async_tick(), f"{DOMAIN}_tick")

    async def async_tick(self) -> None:
        """Meting toevoegen aan de wachtrij en proberen te versturen."""
        when = dt_util.now().replace(microsecond=0)
        measurement = self.measure(when)
        self.last_measurement = measurement
        self.queue.append(measurement)
        await self.async_flush()

    async def async_flush(self) -> None:
        """Wachtrij versturen in pakketjes, oudste eerst; daarna de sensorkeuze synchroniseren."""
        if self._lock.locked():
            return
        async with self._lock:
            if len(self.queue) > QUEUE_MAX:
                del self.queue[: len(self.queue) - QUEUE_MAX]  # oudste vervallen
            server_rev: int | None = None
            new_config: tuple[dict[str, Any], int, str | None] | None = None
            while self.queue:
                batch = self.queue[:BATCH_SIZE]
                meta = {
                    "integration_version": self.version,
                    "ha_version": HA_VERSION,
                    "queue": len(self.queue) - len(batch),
                    "meters": self.meters,
                    "config_rev": self.config_rev,
                    **self.update_status(),
                }
                try:
                    result = await self.api.send(batch, meta)
                except InvalidAuth:
                    self._set_auth_failed()
                    break
                except CannotConnect as err:
                    self._set_offline(str(err))
                    break
                # Verstuurd (afgewezen metingen zijn te oud of onleesbaar; opnieuw sturen heeft geen zin)
                del self.queue[: len(batch)]
                self._set_connected()
                if isinstance(result.get("config_rev"), int):
                    server_rev = result["config_rev"]
                    if isinstance(result.get("config"), dict) and server_rev > self.config_rev:
                        new_config = (result["config"], server_rev, result.get("config_by"))
            self._store.async_delay_save(self._data_to_save, SAVE_DELAY_SECONDS)

            if self.status == STATUS_CONNECTED:
                try:
                    if new_config and not self.entry.options.get(CONF_CONFIG_DIRTY):
                        self._apply_portal_config(*new_config)
                    else:
                        await self._push_config_if_needed(server_rev)
                except InvalidAuth:
                    self._set_auth_failed()
                except CannotConnect as err:
                    _LOGGER.debug("Sensorkeuze synchroniseren lukt nu niet: %s", err)
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

    # ---------- automatisch bijwerken ----------

    @property
    def auto_update(self) -> bool:
        return bool(self.entry.options.get(CONF_AUTO_UPDATE, True))

    def find_update_entity(self) -> str | None:
        """Update-entiteit van HACS voor deze integratie (of None als HACS er niet is)."""
        ent_reg = er.async_get(self.hass)
        for st in self.hass.states.async_all("update"):
            reg = ent_reg.async_get(st.entity_id)
            if reg is None or reg.platform != "hacs":
                continue
            text = " ".join(str(st.attributes.get(k) or "") for k in ("release_url", "title", "friendly_name")).lower()
            if REPO_SLUG in text or "mijn energie" in text:
                return st.entity_id
        return None

    def update_status(self) -> dict[str, Any]:
        """Voor het beheer: staat automatisch bijwerken aan, is er een update, is HACS er?"""
        entity_id = self.find_update_entity()
        st = self.hass.states.get(entity_id) if entity_id else None
        available = st.attributes.get("latest_version") if st is not None and st.state == "on" else None
        return {"auto_update": self.auto_update, "update_available": available, "hacs": entity_id is not None,
                "last_update": self.last_update}

    @callback
    def _auto_update_tick(self, now: datetime | None = None) -> None:
        self.entry.async_create_background_task(self.hass, self.async_auto_update(), f"{DOMAIN}_auto_update")

    async def async_auto_update(self) -> bool:
        """Nieuwe versie installeren via HACS en Home Assistant herstarten. True als er is bijgewerkt."""
        if not self.auto_update:
            return False
        entity_id = self.find_update_entity()
        st = self.hass.states.get(entity_id) if entity_id else None
        if st is None or st.state != "on":
            return False
        version = st.attributes.get("latest_version")
        _LOGGER.warning("Mijn Energie: versie %s wordt automatisch geïnstalleerd; Home Assistant herstart daarna", version)
        self._logbook(f"Versie {version} wordt automatisch geïnstalleerd; Home Assistant herstart daarna.")
        await self.async_stop_queue_save()
        try:
            await self.hass.services.async_call("update", "install", {"entity_id": entity_id}, blocking=True)
        except Exception as err:  # noqa: BLE001 - nooit de integratie laten crashen
            _LOGGER.error("Mijn Energie: automatisch bijwerken mislukt: %s", err)
            self._logbook(f"Automatisch bijwerken naar {version} is mislukt: {err}")
            return False
        self.last_update = f"{version} op {dt_util.now().strftime('%d-%m %H:%M')}"
        await self.hass.services.async_call("homeassistant", "restart", {}, blocking=False)
        return True

    async def async_stop_queue_save(self) -> None:
        """Wachtrij nu opslaan (vóór een herstart)."""
        await self._store.async_save(self._data_to_save())

    # ---------- sensorlijst ----------

    @callback
    def _inventory_tick(self, now: datetime | None = None) -> None:
        self.entry.async_create_background_task(self.hass, self.async_send_inventory(), f"{DOMAIN}_inventory")

    def build_inventory(self) -> list[dict[str, Any]]:
        """Alle sensoren met een eenheid die we kunnen gebruiken (vermogen, energie, gas, %)."""
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        out = []
        for st in self.hass.states.async_all("sensor"):
            unit = st.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
            if unit not in SUPPORTED_UNITS:
                continue
            reg = ent_reg.async_get(st.entity_id)
            if reg is not None and reg.platform == DOMAIN:
                continue
            device = dev_reg.async_get(reg.device_id) if reg is not None and reg.device_id else None
            out.append({
                "e": st.entity_id,
                "n": st.attributes.get(ATTR_FRIENDLY_NAME) or st.entity_id,
                "u": unit,
                "dc": st.attributes.get(ATTR_DEVICE_CLASS),
                "sc": st.attributes.get("state_class"),
                "s": st.state,
                "d": (device.name_by_user or device.name) if device else None,
                "p": reg.platform if reg is not None else None,
            })
        return out

    async def async_send_inventory(self) -> None:
        if self.status == STATUS_AUTH_FAILED:
            return
        try:
            await self.api.send_inventory(self.build_inventory())
            self.last_inventory = dt_util.now()
        except InvalidAuth:
            self._set_auth_failed()
        except CannotConnect as err:
            _LOGGER.debug("Sensorlijst versturen lukt nu niet: %s", err)

    # ---------- status ----------

    def _set_connected(self) -> None:
        if self.status != STATUS_CONNECTED:
            _LOGGER.info("Mijn Energie: verbonden met het portaal")
        self.status = STATUS_CONNECTED
        self.last_sent = dt_util.now()
        self.last_error = None
        self.offline_since = None
        self._reauth_started = False
        ir.async_delete_issue(self.hass, DOMAIN, f"no_connection_{self.entry.entry_id}")

    def _set_offline(self, error: str) -> None:
        now = dt_util.now()
        if self.status != STATUS_OFFLINE:
            _LOGGER.warning("Mijn Energie: portaal niet bereikbaar (%s); metingen wachten", error)
            self.offline_since = now
        self.status = STATUS_OFFLINE
        self.last_error = error
        since = self.offline_since or now
        if (now - since).total_seconds() >= OFFLINE_ISSUE_AFTER_SECONDS:
            ir.async_create_issue(
                self.hass, DOMAIN, f"no_connection_{self.entry.entry_id}",
                is_fixable=False, is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="no_connection",
                translation_placeholders={
                    "since": dt_util.as_local(since).strftime("%d-%m %H:%M"),
                    "queue": str(len(self.queue)),
                    "home": self.entry.title,
                },
            )

    def _set_auth_failed(self) -> None:
        self.status = STATUS_AUTH_FAILED
        self.last_error = "Token geweigerd door het portaal"
        if not self._reauth_started:
            _LOGGER.warning("Mijn Energie: token geweigerd; opnieuw koppelen nodig. Metingen blijven bewaard")
            self._reauth_started = True
            self.entry.async_start_reauth(self.hass)
