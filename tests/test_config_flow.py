"""Koppelschermen, foutmeldingen, opnieuw koppelen en opties."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mijnenergie.api import CannotConnect, InvalidCode, TooManyAttempts
from custom_components.mijnenergie.const import (
    CONF_BATTERY_INVERT,
    CONF_BATTERY_POWER,
    CONF_GAS,
    CONF_HOME_ID,
    CONF_P1_EXPORT,
    CONF_P1_IMPORT,
    CONF_P1_POWER,
    CONF_PV_ENERGY,
    CONF_PV_POWER,
    CONF_TOKEN,
    DOMAIN,
)

from .conftest import ENTRY_DATA, ENTRY_OPTIONS, PAIR_OK

API = "custom_components.mijnenergie.config_flow.MijnEnergieApi"


def _suggested(result, key: str):
    for field in result["data_schema"].schema:
        if str(field) == key:
            return (field.description or {}).get("suggested_value")
    raise KeyError(key)


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_full_flow_with_suggestions(hass: HomeAssistant, meters: dict[str, str]) -> None:
    """Koppelcode → P1 (voorgesteld) → omvormer (voorgesteld) → batterij → klaar."""
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"

    with patch(f"{API}.pair", AsyncMock(return_value=dict(PAIR_OK))) as pair:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "482 913"})
    pair.assert_awaited_once()
    assert pair.await_args.args[0] == "482913"
    assert result["step_id"] == "p1"
    assert result["description_placeholders"]["device"] == "P1 meter"
    # Voorstellen komen van de HomeWizard-sensoren
    assert _suggested(result, CONF_P1_POWER) == meters[CONF_P1_POWER]
    assert _suggested(result, CONF_P1_IMPORT) == meters[CONF_P1_IMPORT]
    assert _suggested(result, CONF_P1_EXPORT) == meters[CONF_P1_EXPORT]
    assert _suggested(result, CONF_GAS) == meters[CONF_GAS]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {k: meters[k] for k in (CONF_P1_POWER, CONF_P1_IMPORT, CONF_P1_EXPORT, CONF_GAS)}
    )
    assert result["step_id"] == "inverter"
    assert _suggested(result, CONF_PV_POWER) == meters[CONF_PV_POWER]
    assert _suggested(result, CONF_PV_ENERGY) == meters[CONF_PV_ENERGY]  # niet "dit jaar"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PV_POWER: meters[CONF_PV_POWER], CONF_PV_ENERGY: meters[CONF_PV_ENERGY]}
    )
    assert result["step_id"] == "battery"

    with patch(f"{API}.send", AsyncMock(return_value={"ok": True, "accepted": 1, "rejected": 0})) as send:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "finish"
    sent = send.await_args.args[0][0]
    assert sent["power"]["pv_w"] == 1240.0
    assert sent["energy"]["pv_kwh"] == 9876.3
    assert send.await_args.args[1]["meters"] == ["p1", "gas", "pv"]

    with patch("custom_components.mijnenergie.MijnEnergieApi.send", AsyncMock(return_value={"ok": True})):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Fam. de Vries · Zwolle"
    assert result["data"][CONF_TOKEN] == "ep_testtoken"
    assert result["data"][CONF_HOME_ID] == 12
    assert result["options"][CONF_PV_ENERGY] == meters[CONF_PV_ENERGY]
    assert result["options"][CONF_BATTERY_POWER] == ""
    assert result["options"][CONF_BATTERY_INVERT] is False
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == "home_12"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_code_errors(hass: HomeAssistant) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123"})
    assert result["errors"] == {"code": "code_format"}

    for exc, errors in (
        (InvalidCode, {"code": "invalid_code"}),
        (TooManyAttempts, {"base": "too_many_attempts"}),
        (CannotConnect("weg"), {"base": "cannot_connect"}),
        (RuntimeError("kapot"), {"base": "unknown"}),
    ):
        with patch(f"{API}.pair", AsyncMock(side_effect=exc)):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "482913"})
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == errors


async def test_p1_validation(hass: HomeAssistant, meters: dict[str, str]) -> None:
    result = await _start(hass)
    with patch(f"{API}.pair", AsyncMock(return_value=dict(PAIR_OK))):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "482913"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_P1_POWER: meters[CONF_P1_IMPORT],  # energie i.p.v. vermogen
            CONF_P1_IMPORT: meters[CONF_P1_POWER],  # momentwaarde i.p.v. meterstand
            CONF_P1_EXPORT: "sensor.bestaat_niet",
        },
    )
    assert result["step_id"] == "p1"
    assert result["errors"] == {
        CONF_P1_POWER: "wrong_unit_power",
        CONF_P1_IMPORT: "wrong_unit_energy",
        CONF_P1_EXPORT: "entity_not_found",
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_P1_POWER: meters[CONF_P1_POWER], CONF_P1_IMPORT: meters[CONF_P1_IMPORT], CONF_P1_EXPORT: meters[CONF_P1_IMPORT]},
    )
    assert result["errors"] == {CONF_P1_EXPORT: "same_entity"}


async def test_meetfase_skips_inverter_and_failed_test(hass: HomeAssistant, meters: dict[str, str]) -> None:
    pair = dict(PAIR_OK, home={"id": 13, "name": "Fam. Peters · Wijhe", "mode": "meetfase"})
    result = await _start(hass)
    with patch(f"{API}.pair", AsyncMock(return_value=pair)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "111111"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {k: meters[k] for k in (CONF_P1_POWER, CONF_P1_IMPORT, CONF_P1_EXPORT)}
    )
    assert result["step_id"] == "battery"
    with patch(f"{API}.send", AsyncMock(side_effect=CannotConnect("geen internet"))):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "finish_failed"
    with patch("custom_components.mijnenergie.MijnEnergieApi.send", AsyncMock(side_effect=CannotConnect("x"))):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_PV_ENERGY] == ""
    assert result["options"][CONF_GAS] == ""
    await hass.config_entries.async_unload(hass.config_entries.async_entries(DOMAIN)[0].entry_id)


async def test_single_instance(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="home_12", data=ENTRY_DATA, options=ENTRY_OPTIONS).add_to_hass(hass)
    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_reauth(hass: HomeAssistant, meters: dict[str, str]) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="home_12", data=ENTRY_DATA, options=ENTRY_OPTIONS, title="Fam. de Vries · Zwolle")
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    # Code van een andere woning
    with patch(f"{API}.pair", AsyncMock(return_value=dict(PAIR_OK, home={"id": 99, "name": "X", "mode": "pv"}))):
        result2 = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "222222"})
    assert result2["type"] is FlowResultType.ABORT and result2["reason"] == "wrong_home"

    result = await entry.start_reauth_flow(hass)
    with (
        patch(f"{API}.pair", AsyncMock(return_value=dict(PAIR_OK, token="ep_nieuw"))),
        patch("custom_components.mijnenergie.MijnEnergieApi.send", AsyncMock(return_value={"ok": True})),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "333333"})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "ep_nieuw"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_options_flow(hass: HomeAssistant, meters: dict[str, str]) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="home_12", data=ENTRY_DATA, options=ENTRY_OPTIONS)
    entry.add_to_hass(hass)
    with patch("custom_components.mijnenergie.MijnEnergieApi.send", AsyncMock(return_value={"ok": True})):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.MENU
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "p1"})
        assert _suggested(result, CONF_GAS) == meters[CONF_GAS]
        # Gas weghalen
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {k: meters[k] for k in (CONF_P1_POWER, CONF_P1_IMPORT, CONF_P1_EXPORT)}
        )
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options[CONF_GAS] == ""
        assert entry.options[CONF_PV_ENERGY] == meters[CONF_PV_ENERGY]  # rest blijft staan

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "relink"})
        assert result["type"] is FlowResultType.ABORT and result["reason"] == "relink_started"
        await hass.async_block_till_done()
        assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())
        await hass.config_entries.async_unload(entry.entry_id)
