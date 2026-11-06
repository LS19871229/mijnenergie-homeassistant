"""Versturen, wachtrij, storingen, hernoemen en opslaan."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.mijnenergie.api import CannotConnect, InvalidAuth
from custom_components.mijnenergie.const import CONF_PV_POWER, DOMAIN

from .conftest import ENTRY_DATA, ENTRY_OPTIONS

SEND = "custom_components.mijnenergie.MijnEnergieApi.send"


def _state(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
    assert entity_id, key
    return hass.states.get(entity_id).state


async def _setup(hass: HomeAssistant, send: AsyncMock) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="home_12", data=ENTRY_DATA, options=ENTRY_OPTIONS, title="Fam. de Vries · Zwolle")
    entry.add_to_hass(hass)
    with patch(SEND, send):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_first_measurement_after_start(hass: HomeAssistant, meters: dict[str, str]) -> None:
    send = AsyncMock(return_value={"ok": True, "accepted": 1, "rejected": 0})
    entry = await _setup(hass, send)
    assert _state(hass, entry, "status") == "starting"
    with patch(SEND, send):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
        await hass.async_block_till_done()
    assert send.await_count == 1
    measurements, meta = send.await_args.args
    m = measurements[0]
    assert m["power"] == {"net_w": -720.0, "pv_w": 1240.0, "battery_w": None}
    assert m["energy"]["pv_kwh"] == 9876.3
    assert m["energy"]["gas_m3"] == 1532.118
    assert meta["meters"] == ["p1", "gas", "pv"]
    assert meta["queue"] == 0
    assert meta["integration_version"] == "1.0.0"
    assert _state(hass, entry, "status") == "connected"
    assert _state(hass, entry, "queue") == "0"
    assert _state(hass, entry, "home") == "Fam. de Vries · Zwolle"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_offline_queue_and_recovery(hass: HomeAssistant, meters: dict[str, str]) -> None:
    send = AsyncMock(side_effect=CannotConnect("geen internet"))
    entry = await _setup(hass, send)
    sender = entry.runtime_data
    with patch(SEND, send):
        for _ in range(3):
            await sender.async_tick()
    assert len(sender.queue) == 3
    assert sender.status == "offline"
    await hass.async_block_till_done()
    assert _state(hass, entry, "status") == "offline"
    assert _state(hass, entry, "queue") == "3"

    # Na 30 minuten een melding onder Reparaties
    sender.offline_since = dt_util.now() - timedelta(minutes=31)
    with patch(SEND, send):
        await sender.async_tick()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"no_connection_{entry.entry_id}") is not None

    # Verbinding terug: alles in één keer, oudste eerst, melding weg
    ok = AsyncMock(return_value={"ok": True, "accepted": 5, "rejected": 0})
    with patch(SEND, ok):
        await sender.async_tick()
    assert ok.await_count == 1
    batch = ok.await_args.args[0]
    assert len(batch) == 5
    assert batch == sorted(batch, key=lambda m: m["ts"])
    assert sender.queue == []
    assert sender.status == "connected"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"no_connection_{entry.entry_id}") is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_batches_of_500(hass: HomeAssistant, meters: dict[str, str]) -> None:
    send = AsyncMock(return_value={"ok": True})
    entry = await _setup(hass, send)
    sender = entry.runtime_data
    sender.queue = [{"ts": f"x{i:05d}"} for i in range(1200)]
    calls: list[list[dict[str, Any]]] = []

    async def capture(batch, meta):
        calls.append(list(batch))
        return {"ok": True}

    with patch(SEND, AsyncMock(side_effect=capture)):
        await sender.async_tick()
    assert [len(c) for c in calls] == [500, 500, 201]
    assert sender.queue == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_queue_capped_at_7_days(hass: HomeAssistant, meters: dict[str, str]) -> None:
    send = AsyncMock(side_effect=CannotConnect("weg"))
    entry = await _setup(hass, send)
    sender = entry.runtime_data
    sender.queue = [{"ts": str(i)} for i in range(7 * 24 * 60 + 50)]
    with patch(SEND, send):
        await sender.async_tick()
    assert len(sender.queue) == 7 * 24 * 60
    assert sender.queue[0]["ts"] == "51"  # oudste vervallen
    await hass.config_entries.async_unload(entry.entry_id)


async def test_token_rejected_starts_relink_and_keeps_data(hass: HomeAssistant, meters: dict[str, str]) -> None:
    send = AsyncMock(side_effect=InvalidAuth)
    entry = await _setup(hass, send)
    sender = entry.runtime_data
    with patch(SEND, send):
        await sender.async_tick()
        await sender.async_tick()
        await hass.async_block_till_done()
    assert sender.status == "auth_failed"
    assert len(sender.queue) == 2
    flows = [f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == "reauth"]
    assert len(flows) == 1  # maar één keer gestart
    await hass.config_entries.async_unload(entry.entry_id)


async def test_entity_rename_is_followed(hass: HomeAssistant, meters: dict[str, str]) -> None:
    send = AsyncMock(return_value={"ok": True})
    entry = await _setup(hass, send)
    with patch(SEND, send):
        er.async_get(hass).async_update_entity(meters[CONF_PV_POWER], new_entity_id="sensor.omvormer_vermogen")
        await hass.async_block_till_done()
    assert entry.options[CONF_PV_POWER] == "sensor.omvormer_vermogen"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_missing_entity_issue_after_grace(hass: HomeAssistant, meters: dict[str, str]) -> None:
    send = AsyncMock(return_value={"ok": True})
    entry = await _setup(hass, send)
    sender = entry.runtime_data
    hass.states.async_remove(meters[CONF_PV_POWER])
    issue_id = f"entity_missing_{entry.entry_id}_{CONF_PV_POWER}"
    with patch(SEND, send):
        await sender.async_tick()
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None  # nog binnen 10 minuten
        sender._missing_since[CONF_PV_POWER] = dt_util.now() - timedelta(minutes=11)
        await sender.async_tick()
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
        assert sender.last_measurement["power"]["pv_w"] is None
        hass.states.async_set(meters[CONF_PV_POWER], "2", {"unit_of_measurement": "kW"})
        await sender.async_tick()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert sender.last_measurement["power"]["pv_w"] == 2000.0
    await hass.config_entries.async_unload(entry.entry_id)


async def test_queue_survives_restart(hass: HomeAssistant, hass_storage: dict[str, Any], meters: dict[str, str]) -> None:
    send = AsyncMock(side_effect=CannotConnect("weg"))
    entry = await _setup(hass, send)
    with patch(SEND, send):
        await entry.runtime_data.async_tick()
        await entry.runtime_data.async_tick()
    await hass.config_entries.async_unload(entry.entry_id)
    key = f"{DOMAIN}.{entry.entry_id}.queue"
    assert len(hass_storage[key]["data"]["queue"]) == 2

    with patch(SEND, send):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert len(entry.runtime_data.queue) == 2
    await hass.config_entries.async_unload(entry.entry_id)

    # Integratie verwijderen ruimt de opslag op
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_diagnostics_hide_token(hass: HomeAssistant, meters: dict[str, str]) -> None:
    from custom_components.mijnenergie.diagnostics import async_get_config_entry_diagnostics

    send = AsyncMock(return_value={"ok": True})
    entry = await _setup(hass, send)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["data"]["token"] == "**REDACTED**"
    assert "ep_testtoken" not in str(diag)
    assert diag["sensors"][CONF_PV_POWER]["unit"] == "kW"
    await hass.config_entries.async_unload(entry.entry_id)
