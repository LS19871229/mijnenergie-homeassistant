"""Versturen, wachtrij, storingen, synchronisatie met het portaal, sensorlijst, hernoemen, migratie en opslaan."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.mijnenergie.api import CannotConnect, InvalidAuth
from custom_components.mijnenergie.const import CONF_CONFIG, CONF_CONFIG_DIRTY, CONF_CONFIG_REV, DOMAIN

from .conftest import API, CONFIG_INVERTER, ENTRY_DATA, ENTRY_OPTIONS

MANIFEST_VERSION = json.loads((Path(__file__).parents[1] / "custom_components/mijnenergie/manifest.json").read_text())["version"]
OK = {"ok": True, "accepted": 1, "rejected": 0, "config_rev": 3}


def _state(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
    assert entity_id, key
    return hass.states.get(entity_id).state


async def _setup(hass: HomeAssistant, options: dict[str, Any] | None = None, minor: int = 2, send: AsyncMock | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="home_12", data=ENTRY_DATA, options=options or ENTRY_OPTIONS,
                            title="Fam. de Vries · Zwolle", minor_version=minor)
    entry.add_to_hass(hass)
    with patch(f"{API}.send", send or AsyncMock(return_value=OK)), patch(f"{API}.send_inventory", AsyncMock(return_value=1)), \
         patch(f"{API}.push_config", AsyncMock(return_value=1)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_first_measurement_after_start(hass: HomeAssistant, meters: dict[str, str]) -> None:
    send = AsyncMock(return_value=OK)
    entry = await _setup(hass, send=send)
    with patch(f"{API}.send", send), patch(f"{API}.send_inventory", AsyncMock(return_value=1)):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
        await hass.async_block_till_done()
    measurements, meta = send.await_args.args
    m = measurements[0]
    assert m["power"]["net_w"] == -720.0 and m["power"]["pv_w"] == 1240.0 and m["power"]["battery_w"] is None
    assert m["energy"]["pv_kwh"] == 9876.3 and m["energy"]["gas_m3"] == 1532.118
    assert meta["meters"] == ["p1", "gas", "pv"] and meta["config_rev"] == 3 and meta["integration_version"] == MANIFEST_VERSION
    assert _state(hass, entry, "status") == "connected"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_offline_queue_and_recovery(hass: HomeAssistant, meters: dict[str, str]) -> None:
    send = AsyncMock(side_effect=CannotConnect("geen internet"))
    entry = await _setup(hass, send=send)
    sender = entry.runtime_data
    with patch(f"{API}.send", send):
        for _ in range(3):
            await sender.async_tick()
    assert len(sender.queue) >= 3 and sender.status == "offline"
    sender.offline_since = dt_util.now() - timedelta(minutes=31)
    with patch(f"{API}.send", send):
        await sender.async_tick()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"no_connection_{entry.entry_id}") is not None
    ok = AsyncMock(return_value=OK)
    with patch(f"{API}.send", ok):
        await sender.async_tick()
    batch = ok.await_args.args[0]
    assert batch == sorted(batch, key=lambda m: m["ts"]) and sender.queue == []
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"no_connection_{entry.entry_id}") is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_batches_and_cap(hass: HomeAssistant, meters: dict[str, str]) -> None:
    entry = await _setup(hass)
    sender = entry.runtime_data
    sender.queue = [{"ts": f"x{i:05d}"} for i in range(1200)]
    calls: list[int] = []

    async def capture(batch, meta):
        calls.append(len(batch))
        return OK

    with patch(f"{API}.send", AsyncMock(side_effect=capture)):
        await sender.async_tick()
    assert calls == [500, 500, 201]
    sender.queue = [{"ts": str(i)} for i in range(7 * 24 * 60 + 50)]
    with patch(f"{API}.send", AsyncMock(side_effect=CannotConnect("weg"))):
        await sender.async_tick()
    assert len(sender.queue) == 7 * 24 * 60
    await hass.config_entries.async_unload(entry.entry_id)


async def test_token_rejected_starts_relink_once(hass: HomeAssistant, meters: dict[str, str]) -> None:
    entry = await _setup(hass)
    sender = entry.runtime_data
    with patch(f"{API}.send", AsyncMock(side_effect=InvalidAuth)):
        await sender.async_tick()
        await sender.async_tick()
        await hass.async_block_till_done()
    assert sender.status == "auth_failed" and len(sender.queue) == 2
    assert len([f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == "reauth"]) == 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_portal_config_is_applied(hass: HomeAssistant, hybrid: dict[str, str]) -> None:
    """Nieuwe sensorkeuze in het portaal -> binnen een minuut actief op de HA Green."""
    entry = await _setup(hass)
    sender = entry.runtime_data
    portal = {"installation": "hybrid", "p1": dict(CONFIG_INVERTER["p1"]),
              "pv": [{"label": "", "power": hybrid["inv_input"], "energy": hybrid["inv_pv_total"]}],
              "battery": [{"label": "", "power": hybrid["bat_power"], "soc": hybrid["bat_soc"], "charge": "", "discharge": "", "invert": False, "capacity": 10}],
              "house": {"power": ""}}
    with patch(f"{API}.send", AsyncMock(return_value={**OK, "config_rev": 4, "config": portal, "config_by": "Leo"})):
        await sender.async_tick()
    assert entry.options[CONF_CONFIG_REV] == 4 and entry.options[CONF_CONFIG]["installation"] == "hybrid"
    send = AsyncMock(return_value={**OK, "config_rev": 4})
    with patch(f"{API}.send", send), patch(f"{API}.push_config", AsyncMock()) as push:
        await sender.async_tick()
    m, meta = send.await_args.args
    assert m[-1]["power"]["pv_w"] == 3200.0 and m[-1]["power"]["battery_w"] == 1500.0 and m[-1]["battery_soc"] == 55
    assert meta["config_rev"] == 4 and meta["meters"] == ["p1", "gas", "pv", "battery"]
    push.assert_not_awaited()  # niets terug te sturen
    await hass.config_entries.async_unload(entry.entry_id)


async def test_local_config_pushed_when_portal_has_none(hass: HomeAssistant, meters: dict[str, str]) -> None:
    entry = await _setup(hass, options={**ENTRY_OPTIONS, CONF_CONFIG_REV: 0})
    with patch(f"{API}.send", AsyncMock(return_value={**OK, "config_rev": 0})), patch(f"{API}.push_config", AsyncMock(return_value=1)) as push:
        await entry.runtime_data.async_tick()
    push.assert_awaited_once()
    assert entry.options[CONF_CONFIG_REV] == 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_dirty_local_change_wins_over_portal(hass: HomeAssistant, meters: dict[str, str]) -> None:
    """Lokaal gewijzigd en nog niet verstuurd: eerst versturen (laatste wijziging wint)."""
    entry = await _setup(hass, options={**ENTRY_OPTIONS, CONF_CONFIG_DIRTY: True})
    with patch(f"{API}.send", AsyncMock(return_value={**OK, "config_rev": 9, "config": {"installation": "none", "p1": {}}})), \
         patch(f"{API}.push_config", AsyncMock(return_value=10)) as push:
        await entry.runtime_data.async_tick()
    push.assert_awaited_once()
    assert entry.options[CONF_CONFIG]["installation"] == "inverter" and entry.options[CONF_CONFIG_REV] == 10
    await hass.config_entries.async_unload(entry.entry_id)


async def test_inventory(hass: HomeAssistant, hybrid: dict[str, str]) -> None:
    entry = await _setup(hass)
    inv = AsyncMock(return_value=10)
    with patch(f"{API}.send_inventory", inv):
        await entry.runtime_data.async_send_inventory()
    items = {e["e"]: e for e in inv.await_args.args[0]}
    assert items[hybrid["inv_input"]]["u"] == "W" and items[hybrid["inv_input"]]["p"] == "huawei_solar"
    assert items[hybrid["p1_power"]]["d"] == "P1 meter"
    assert not any(e.startswith("sensor.mijn_energie") for e in items)  # eigen sensoren niet
    await hass.config_entries.async_unload(entry.entry_id)


async def test_entity_rename_is_followed_and_pushed(hass: HomeAssistant, meters: dict[str, str]) -> None:
    entry = await _setup(hass)
    er.async_get(hass).async_update_entity(meters["pv_power"], new_entity_id="sensor.omvormer_vermogen")
    await hass.async_block_till_done()
    assert entry.options[CONF_CONFIG]["pv"][0]["power"] == "sensor.omvormer_vermogen"
    assert entry.options[CONF_CONFIG_DIRTY] is True
    await hass.config_entries.async_unload(entry.entry_id)


async def test_missing_entity_issue_after_grace(hass: HomeAssistant, meters: dict[str, str]) -> None:
    entry = await _setup(hass)
    sender = entry.runtime_data
    hass.states.async_remove(meters["pv_power"])
    issue_id = f"entity_missing_{entry.entry_id}_pv.0.power"
    with patch(f"{API}.send", AsyncMock(return_value=OK)):
        await sender.async_tick()
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
        sender._missing_since["pv.0.power"] = dt_util.now() - timedelta(minutes=11)
        await sender.async_tick()
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
        hass.states.async_set(meters["pv_power"], "2", {"unit_of_measurement": "kW"})
        await sender.async_tick()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_migration_from_v1(hass: HomeAssistant, meters: dict[str, str]) -> None:
    v1 = {"p1_power": meters["p1_power"], "p1_import": meters["p1_import"], "p1_export": meters["p1_export"], "gas": "",
          "pv_power": meters["pv_power"], "pv_energy": meters["pv_energy"], "battery_power": "", "battery_soc": "", "battery_invert": False}
    entry = await _setup(hass, options=v1, minor=1)
    assert entry.minor_version == 2
    cfg = entry.options[CONF_CONFIG]
    assert cfg["installation"] == "inverter" and cfg["pv"][0]["power"] == meters["pv_power"]
    assert entry.options[CONF_CONFIG_DIRTY] is True and entry.options[CONF_CONFIG_REV] == 0
    await hass.config_entries.async_unload(entry.entry_id)


async def test_queue_survives_restart(hass: HomeAssistant, hass_storage: dict[str, Any], meters: dict[str, str]) -> None:
    send = AsyncMock(side_effect=CannotConnect("weg"))
    entry = await _setup(hass, send=send)
    with patch(f"{API}.send", send):
        await entry.runtime_data.async_tick()
    await hass.config_entries.async_unload(entry.entry_id)
    key = f"{DOMAIN}.{entry.entry_id}.queue"
    n = len(hass_storage[key]["data"]["queue"])
    assert n >= 1
    with patch(f"{API}.send", send), patch(f"{API}.send_inventory", AsyncMock(return_value=1)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert len(entry.runtime_data.queue) == n
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_diagnostics_hide_token(hass: HomeAssistant, meters: dict[str, str]) -> None:
    from custom_components.mijnenergie.diagnostics import async_get_config_entry_diagnostics

    entry = await _setup(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["data"]["token"] == "**REDACTED**" and "ep_testtoken" not in str(diag)
    assert diag["sensors"]["pv.0.power"]["unit"] == "kW" and diag["config_rev"] == 3
    await hass.config_entries.async_unload(entry.entry_id)
