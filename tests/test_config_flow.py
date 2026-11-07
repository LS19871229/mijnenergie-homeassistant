"""Koppelschermen, installatietypes, foutmeldingen, overnemen uit het portaal, opnieuw koppelen en Configureren."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mijnenergie.api import CannotConnect, InvalidCode, TooManyAttempts
from custom_components.mijnenergie.const import CONF_CONFIG, CONF_CONFIG_DIRTY, CONF_CONFIG_REV, CONF_TOKEN, DOMAIN

from .conftest import API, CONFIG_INVERTER, ENTRY_DATA, ENTRY_OPTIONS, PAIR_OK


def _suggested(result, key: str):
    for field in result["data_schema"].schema:
        if str(field) == key:
            return (field.description or {}).get("suggested_value")
    raise KeyError(key)


def _keys(result) -> list[str]:
    return [str(f) for f in result["data_schema"].schema]


async def _paired(hass: HomeAssistant, pair: dict[str, Any] | None = None):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(f"{API}.pair", AsyncMock(return_value=dict(pair or PAIR_OK))):
        return await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "482 913"})


async def _p1(hass: HomeAssistant, flow_id: str, m: dict[str, str]):
    return await hass.config_entries.flow.async_configure(
        flow_id, {"p1_power": m["p1_power"], "p1_import": m["p1_import"], "p1_export": m["p1_export"], "gas": m["gas"]})


async def _finish(hass: HomeAssistant, result, rev: int = 7, send_ok: bool = True):
    send = AsyncMock(return_value={"ok": True, "config_rev": 0}) if send_ok else AsyncMock(side_effect=CannotConnect("weg"))
    with patch(f"{API}.send", send), patch(f"{API}.push_config", AsyncMock(return_value=rev)) as push, \
         patch(f"{API}.send_inventory", AsyncMock(return_value=1)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        step = result.get("step_id")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    return step, result, push, send


async def test_inverter_flow(hass: HomeAssistant, meters: dict[str, str]) -> None:
    result = await _paired(hass)
    assert result["step_id"] == "installation"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"installation": "inverter"})
    assert result["step_id"] == "p1"
    assert result["description_placeholders"]["device"] == "P1 meter"
    assert _suggested(result, "p1_power") == meters["p1_power"]
    assert _suggested(result, "gas") == meters["gas"]
    result = await _p1(hass, result["flow_id"], meters)
    assert result["step_id"] == "pv"
    assert _suggested(result, "pv_power_1") == meters["pv_power"]
    assert _suggested(result, "pv_energy_1") == meters["pv_energy"]  # niet "dit jaar"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pv_power_1": meters["pv_power"], "pv_energy_1": meters["pv_energy"]})
    assert result["step_id"] == "house"  # geen batterijstap bij een omvormer zonder batterij
    step, result, push, send = await _finish(hass, result)
    assert step == "finish"
    assert result["type"] is FlowResultType.CREATE_ENTRY
    cfg = result["options"][CONF_CONFIG]
    assert cfg["installation"] == "inverter"
    assert cfg["pv"] == [{"label": "", "power": meters["pv_power"], "energy": meters["pv_energy"]}]
    assert result["options"][CONF_CONFIG_REV] == 7 and result["options"][CONF_CONFIG_DIRTY] is False
    assert push.await_args.args[0]["p1"]["power"] == meters["p1_power"]
    assert send.await_args.args[0][0]["power"]["pv_w"] == 1240.0
    await hass.config_entries.async_unload(hass.config_entries.async_entries(DOMAIN)[0].entry_id)


async def test_hybrid_flow_suggests_pv_input_not_output(hass: HomeAssistant, hybrid: dict[str, str]) -> None:
    result = await _paired(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"installation": "hybrid"})
    result = await _p1(hass, result["flow_id"], hybrid)
    assert result["step_id"] == "pv_hybrid"
    assert _suggested(result, "pv_power_1") == hybrid["inv_input"]  # niet inverter_active_power (uitgang)
    assert _suggested(result, "pv_energy_1") == hybrid["inv_pv_total"]  # niet de dagopbrengst
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pv_power_1": hybrid["inv_input"], "pv_energy_1": hybrid["inv_pv_total"]})
    assert result["step_id"] == "battery"
    assert _suggested(result, "battery_power") == hybrid["bat_power"]
    assert _suggested(result, "battery_soc") == hybrid["bat_soc"]
    assert _suggested(result, "battery_charge") == hybrid["bat_charge"]
    assert _suggested(result, "battery_discharge") == hybrid["bat_discharge"]
    # Eén teller kiezen mag niet
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "battery_power": hybrid["bat_power"], "battery_soc": hybrid["bat_soc"], "battery_charge": hybrid["bat_charge"]})
    assert result["errors"] == {"battery_discharge": "both_counters"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "battery_power": hybrid["bat_power"], "battery_soc": hybrid["bat_soc"], "battery_charge": hybrid["bat_charge"],
        "battery_discharge": hybrid["bat_discharge"], "battery_capacity": 10})
    assert result["step_id"] == "house"
    assert _suggested(result, "house_power") == hybrid["house"]
    send = AsyncMock(return_value={"ok": True, "config_rev": 0})
    with patch(f"{API}.send", send), patch(f"{API}.push_config", AsyncMock(return_value=1)), patch(f"{API}.send_inventory", AsyncMock(return_value=1)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"house_power": hybrid["house"]})
        assert result["step_id"] == "finish"
        test_m = send.await_args.args[0][0]
        assert test_m["power"]["pv_w"] == 3200.0 and test_m["power"]["house_w"] == 1010.0
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    cfg = result["options"][CONF_CONFIG]
    assert cfg["battery"][0]["capacity"] == 10.0 and cfg["battery"][0]["charge"] == hybrid["bat_charge"]
    assert cfg["house"]["power"] == hybrid["house"]
    await hass.config_entries.async_unload(hass.config_entries.async_entries(DOMAIN)[0].entry_id)


async def test_multi_inverter_flow(hass: HomeAssistant, hybrid: dict[str, str]) -> None:
    result = await _paired(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"installation": "multi"})
    result = await _p1(hass, result["flow_id"], hybrid)
    assert result["step_id"] == "pv_multi"
    assert "pv_power_3" in _keys(result) and "pv_label_2" in _keys(result)
    # Zelfde sensor twee keer
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "pv_power_1": hybrid["inv_input"], "pv_energy_1": hybrid["inv_pv_total"],
        "pv_power_2": hybrid["inv_input"], "pv_energy_2": hybrid["pv_energy"]})
    assert result["errors"] == {"pv_power_2": "duplicate"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "pv_label_1": "Huawei", "pv_power_1": hybrid["inv_input"], "pv_energy_1": hybrid["inv_pv_total"],
        "pv_label_2": "SolarEdge", "pv_power_2": hybrid["pv_power"], "pv_energy_2": hybrid["pv_energy"]})
    assert result["step_id"] == "battery"  # optioneel bij meerdere omvormers
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "house"
    _, result, _, _ = await _finish(hass, result)
    cfg = result["options"][CONF_CONFIG]
    assert [p["label"] for p in cfg["pv"]] == ["Huawei", "SolarEdge"]
    assert cfg["battery"] == []
    await hass.config_entries.async_unload(hass.config_entries.async_entries(DOMAIN)[0].entry_id)


async def test_meetfase_preselects_none(hass: HomeAssistant, meters: dict[str, str]) -> None:
    result = await _paired(hass, dict(PAIR_OK, home={"id": 13, "name": "Fam. Peters", "mode": "meetfase"}))
    assert result["step_id"] == "installation"
    field = next(f for f in result["data_schema"].schema if str(f) == "installation")
    assert field.default() == "none"


async def test_no_internet_on_finish(hass: HomeAssistant, meters: dict[str, str]) -> None:
    result = await _paired(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"installation": "none"})
    with patch(f"{API}.send", AsyncMock(side_effect=CannotConnect("geen internet"))):
        result = await _p1(hass, result["flow_id"], meters)
    assert result["step_id"] == "finish_failed"
    with patch(f"{API}.send", AsyncMock(side_effect=CannotConnect("x"))), patch(f"{API}.send_inventory", AsyncMock(side_effect=CannotConnect("x"))):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_CONFIG_DIRTY] is True  # wordt later alsnog naar het portaal gestuurd
    assert result["options"][CONF_CONFIG_REV] == 0
    await hass.config_entries.async_unload(hass.config_entries.async_entries(DOMAIN)[0].entry_id)


async def test_adopt_config_from_portal(hass: HomeAssistant, meters: dict[str, str]) -> None:
    pair = dict(PAIR_OK, config_rev=5, config=CONFIG_INVERTER)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(f"{API}.pair", AsyncMock(return_value=pair)), patch(f"{API}.send", AsyncMock(return_value={"ok": True, "config_rev": 5})), \
         patch(f"{API}.push_config", AsyncMock()) as push:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "482913"})
    assert result["step_id"] == "finish_portal"
    assert result["description_placeholders"]["rev"] == "5"
    push.assert_not_awaited()
    with patch(f"{API}.send", AsyncMock(return_value={"ok": True, "config_rev": 5})), patch(f"{API}.send_inventory", AsyncMock(return_value=1)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["options"][CONF_CONFIG_REV] == 5 and result["options"][CONF_CONFIG_DIRTY] is False
    await hass.config_entries.async_unload(hass.config_entries.async_entries(DOMAIN)[0].entry_id)


async def test_code_and_p1_errors(hass: HomeAssistant, meters: dict[str, str]) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123"})
    assert result["errors"] == {"code": "code_format"}
    for exc, errors in ((InvalidCode, {"code": "invalid_code"}), (TooManyAttempts, {"base": "too_many_attempts"}),
                        (CannotConnect("weg"), {"base": "cannot_connect"}), (RuntimeError("kapot"), {"base": "unknown"})):
        with patch(f"{API}.pair", AsyncMock(side_effect=exc)):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "482913"})
        assert result["errors"] == errors
    with patch(f"{API}.pair", AsyncMock(return_value=dict(PAIR_OK))):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "482913"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"installation": "inverter"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "p1_power": meters["p1_import"], "p1_import": meters["p1_power"], "p1_export": "sensor.bestaat_niet"})
    assert result["errors"] == {"p1_power": "wrong_unit_power", "p1_import": "wrong_unit_energy", "p1_export": "entity_not_found"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "p1_power": meters["p1_power"], "p1_import": meters["p1_import"], "p1_export": meters["p1_import"]})
    assert result["errors"] == {"p1_export": "same_entity"}


async def test_single_instance(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="home_12", data=ENTRY_DATA, options=ENTRY_OPTIONS, minor_version=2).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "single_instance_allowed"


async def test_reauth(hass: HomeAssistant, meters: dict[str, str]) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="home_12", data=ENTRY_DATA, options=ENTRY_OPTIONS, minor_version=2, title="Fam. de Vries · Zwolle")
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    with patch(f"{API}.pair", AsyncMock(return_value=dict(PAIR_OK, home={"id": 99, "name": "X", "mode": "pv"}))):
        result2 = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "222222"})
    assert result2["reason"] == "wrong_home"
    result = await entry.start_reauth_flow(hass)
    with patch(f"{API}.pair", AsyncMock(return_value=dict(PAIR_OK, token="ep_nieuw"))), \
         patch(f"{API}.send", AsyncMock(return_value={"ok": True, "config_rev": 3})), patch(f"{API}.send_inventory", AsyncMock(return_value=1)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "333333"})
        await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "ep_nieuw"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_options_flow_changes_go_to_portal(hass: HomeAssistant, hybrid: dict[str, str]) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="home_12", data=ENTRY_DATA, options=ENTRY_OPTIONS, minor_version=2)
    entry.add_to_hass(hass)
    with patch(f"{API}.send", AsyncMock(return_value={"ok": True, "config_rev": 3})), patch(f"{API}.send_inventory", AsyncMock(return_value=1)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.MENU and result["description_placeholders"]["rev"] == "3"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "installation"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"installation": "hybrid"})
        assert _suggested(result, "p1_power") == hybrid["p1_power"]  # huidige keuze blijft staan
        result = await hass.config_entries.options.async_configure(result["flow_id"], {
            "p1_power": hybrid["p1_power"], "p1_import": hybrid["p1_import"], "p1_export": hybrid["p1_export"]})
        assert result["step_id"] == "pv_hybrid"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"pv_power_1": hybrid["inv_input"], "pv_energy_1": hybrid["inv_pv_total"]})
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"battery_power": hybrid["bat_power"], "battery_soc": hybrid["bat_soc"]})
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_CONFIG]["installation"] == "hybrid"
    assert entry.options[CONF_CONFIG]["p1"]["gas"] == ""  # leeggemaakt
    assert entry.options[CONF_CONFIG_DIRTY] is True

    # Volgende verzending stuurt de nieuwe keuze naar het portaal
    with patch(f"{API}.send", AsyncMock(return_value={"ok": True, "config_rev": 3})), patch(f"{API}.push_config", AsyncMock(return_value=4)) as push:
        await entry.runtime_data.async_tick()
    assert push.await_args.args[0]["installation"] == "hybrid"
    assert entry.options[CONF_CONFIG_REV] == 4 and entry.options[CONF_CONFIG_DIRTY] is False

    with patch(f"{API}.send", AsyncMock(return_value={"ok": True})), patch(f"{API}.send_inventory", AsyncMock(return_value=1)):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "relink"})
        await hass.async_block_till_done()
    assert result["reason"] == "relink_started"
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())
    await hass.config_entries.async_unload(entry.entry_id)
