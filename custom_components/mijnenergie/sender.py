"""Elke minuut meten, in de wachtrij zetten en naar het portaal sturen."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import CannotConnect, InvalidAuth, MijnEnergieApi
from .const import (
    BATCH_SIZE,
    CONF_BATTERY_INVERT,
    DOMAIN,
    METERS,
    MISSING_ENTITY_GRACE_SECONDS,
    OFFLINE_ISSUE_AFTER_SECONDS,
    QUEUE_MAX,
    SAVE_DELAY_SECONDS,
    SENSOR_KINDS,
    STATUS_AUTH_FAILED,
    STATUS_CONNECTED,
    STATUS_OFFLINE,
    STATUS_STARTING,
    STORAGE_VERSION,
    signal_update,
)
from .measure import build_measurement

_LOGGER = logging.getLogger(__name__)


def queue_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Opslag van de wachtrij (blijft bewaard na een herstart)."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.queue")


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
        self._store = queue_store(hass, entry.entry_id)
        self._lock = asyncio.Lock()
        self._unsub: list[CALLBACK_TYPE] = []
        self._missing_since: dict[str, datetime] = {}
        self._reauth_started = False

    # ---------- levensloop ----------

    async def async_start(self) -> None:
        """Wachtrij laden en de minuuttimer starten."""
        stored = await self._store.async_load()
        if isinstance(stored, dict) and isinstance(stored.get("queue"), list):
            self.queue = [m for m in stored["queue"] if isinstance(m, dict)][-QUEUE_MAX:]
        self._unsub.append(async_track_time_change(self.hass, self._tick, second=0))
        # Meteen na het starten een eerste meting, zodat je niet een minuut hoeft te wachten
        self._unsub.append(async_call_later(self.hass, 5, self._tick))

    async def async_stop(self) -> None:
        """Timer stoppen en de wachtrij direct opslaan."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        await self._store.async_save(self._data_to_save())

    def _data_to_save(self) -> dict[str, Any]:
        return {"queue": self.queue}

    # ---------- meten ----------

    @property
    def selected(self) -> dict[str, str]:
        """Gekozen sensoren: sleutel -> entity_id (alleen ingevulde)."""
        return {k: v for k, v in self.entry.options.items() if k in SENSOR_KINDS and v}

    @property
    def meters(self) -> list[str]:
        sel = self.selected
        return [name for name, keys in METERS.items() if any(k in sel for k in keys)]

    def measure(self, when: datetime) -> dict[str, Any]:
        """Lees de gekozen sensoren en bouw een meting."""
        readings: dict[str, tuple[Any, str | None]] = {}
        for key, entity_id in self.selected.items():
            state = self.hass.states.get(entity_id)
            self._track_missing(key, entity_id, state is None, when)
            if state is not None:
                readings[key] = (state.state, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
        return build_measurement(when, readings, bool(self.entry.options.get(CONF_BATTERY_INVERT)))

    def _track_missing(self, key: str, entity_id: str, missing: bool, when: datetime) -> None:
        """Melding als een gekozen sensor langer dan 10 minuten niet bestaat."""
        issue_id = f"entity_missing_{self.entry.entry_id}_{key}"
        if not missing:
            if self._missing_since.pop(key, None) is not None:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        since = self._missing_since.setdefault(key, when)
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
        """Wachtrij versturen in pakketjes, oudste eerst."""
        if self._lock.locked():
            return
        async with self._lock:
            if len(self.queue) > QUEUE_MAX:
                del self.queue[: len(self.queue) - QUEUE_MAX]  # oudste vervallen
            while self.queue:
                batch = self.queue[:BATCH_SIZE]
                meta = {
                    "integration_version": self.version,
                    "ha_version": HA_VERSION,
                    "queue": len(self.queue) - len(batch),
                    "meters": self.meters,
                }
                try:
                    await self.api.send(batch, meta)
                except InvalidAuth:
                    self._set_auth_failed()
                    break
                except CannotConnect as err:
                    self._set_offline(str(err))
                    break
                # Verstuurd (afgewezen metingen zijn te oud of onleesbaar; opnieuw sturen heeft geen zin)
                del self.queue[: len(batch)]
                self._set_connected()
            self._store.async_delay_save(self._data_to_save, SAVE_DELAY_SECONDS)
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

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
