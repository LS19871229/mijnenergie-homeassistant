"""Koppelschermen: koppelcode, P1-meter, omvormer, batterij; plus opties en opnieuw koppelen."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .api import CannotConnect, InvalidAuth, InvalidCode, MijnEnergieApi, TooManyAttempts, normalize_url
from .const import (
    CONF_BATTERY_INVERT,
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_CODE,
    CONF_GAS,
    CONF_HOME_ID,
    CONF_HOME_MODE,
    CONF_HOME_NAME,
    CONF_P1_EXPORT,
    CONF_P1_IMPORT,
    CONF_P1_POWER,
    CONF_PV_ENERGY,
    CONF_PV_POWER,
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_URL,
    DOMAIN,
    METERS,
    MODE_MEETFASE,
    SENSOR_KINDS,
)
from .detect import suggest_battery, suggest_p1, suggest_pv
from .measure import build_measurement, unit_supported

_LOGGER = logging.getLogger(__name__)

P1_KEYS = (CONF_P1_POWER, CONF_P1_IMPORT, CONF_P1_EXPORT, CONF_GAS)
P1_REQUIRED = (CONF_P1_POWER, CONF_P1_IMPORT, CONF_P1_EXPORT)
PV_KEYS = (CONF_PV_POWER, CONF_PV_ENERGY)
PV_REQUIRED = (CONF_PV_ENERGY,)
BATTERY_KEYS = (CONF_BATTERY_POWER, CONF_BATTERY_SOC)

SENSOR_SELECTOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))


async def _versions(hass: HomeAssistant) -> tuple[str, str]:
    integration = await async_get_integration(hass, DOMAIN)
    return str(integration.version or "0"), HA_VERSION


def _clean_code(code: str) -> str:
    return "".join(ch for ch in str(code) if ch.isdigit())


def _entity_schema(keys: tuple[str, ...], required: tuple[str, ...], values: Mapping[str, Any]) -> dict:
    """Keuzevelden voor sensoren, met huidige keuze of voorstel al ingevuld."""
    fields: dict = {}
    for key in keys:
        marker = vol.Required if key in required else vol.Optional
        current = values.get(key)
        fields[marker(key, description={"suggested_value": current} if current else None)] = SENSOR_SELECTOR
    return fields


def _validate(hass: HomeAssistant, user_input: Mapping[str, Any], keys: tuple[str, ...], required: tuple[str, ...]) -> dict[str, str]:
    """Bestaat de sensor, kunnen we de eenheid omrekenen, is het een totaal?"""
    errors: dict[str, str] = {}
    for key in keys:
        entity_id = user_input.get(key)
        if not entity_id:
            if key in required:
                errors[key] = "required"
            continue
        state = hass.states.get(entity_id)
        if state is None:
            errors[key] = "entity_not_found"
            continue
        kind = SENSOR_KINDS[key]
        if not unit_supported(kind, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)):
            errors[key] = f"wrong_unit_{kind}"
        elif kind in ("energy", "gas") and state.attributes.get("state_class") == "measurement":
            errors[key] = "not_a_total"
    if (
        CONF_P1_IMPORT in keys
        and user_input.get(CONF_P1_IMPORT)
        and user_input.get(CONF_P1_IMPORT) == user_input.get(CONF_P1_EXPORT)
    ):
        errors[CONF_P1_EXPORT] = "same_entity"
    return errors


def _picked(user_input: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Alleen de velden van dit scherm; leeggemaakte velden worden ''."""
    return {key: user_input.get(key) or "" for key in keys}


def _battery_schema(values: Mapping[str, Any]) -> vol.Schema:
    fields = _entity_schema(BATTERY_KEYS, (), values)
    fields[vol.Optional(CONF_BATTERY_INVERT, default=bool(values.get(CONF_BATTERY_INVERT)))] = selector.BooleanSelector()
    return vol.Schema(fields)


class MijnEnergieConfigFlow(ConfigFlow, domain=DOMAIN):
    """Koppelen in vier stappen."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._p1_device: str | None = None
        self._test_error: str | None = None

    # ---------- stap 1: koppelcode ----------

    async def _async_pair(self, url: str, code: str) -> dict[str, Any]:
        integration_version, ha_version = await _versions(self.hass)
        api = MijnEnergieApi(async_get_clientsession(self.hass), url)
        return await api.pair(code, integration_version, ha_version)

    async def _code_form_errors(self, user_input: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, str]]:
        errors: dict[str, str] = {}
        code = _clean_code(user_input.get(CONF_CODE, ""))
        url = normalize_url(user_input.get(CONF_URL) or self._data.get(CONF_URL) or DEFAULT_URL)
        if len(code) != 6:
            return None, {CONF_CODE: "code_format"}
        try:
            result = await self._async_pair(url, code)
        except InvalidCode:
            errors[CONF_CODE] = "invalid_code"
        except TooManyAttempts:
            errors["base"] = "too_many_attempts"
        except CannotConnect as err:
            _LOGGER.warning("Koppelen met %s mislukt: %s", url, err)
            errors["base"] = "cannot_connect"
        except Exception:  # noqa: BLE001 - nooit de flow laten crashen
            _LOGGER.exception("Onverwachte fout bij koppelen")
            errors["base"] = "unknown"
        else:
            result["url"] = url
            return result, {}
        return None, errors

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            result, errors = await self._code_form_errors(user_input)
            if result:
                home = result["home"]
                self._data = {
                    CONF_URL: result["url"],
                    CONF_TOKEN: result["token"],
                    CONF_HOME_ID: int(home["id"]),
                    CONF_HOME_NAME: str(home.get("name") or f"Woning {home['id']}"),
                    CONF_HOME_MODE: str(home.get("mode") or "pv"),
                }
                await self.async_set_unique_id(f"home_{home['id']}")
                # Al gekoppeld op deze HA? Dan alleen het token vernieuwen.
                self._abort_if_unique_id_configured(updates={CONF_TOKEN: result["token"], CONF_URL: result["url"]})
                return await self.async_step_p1()

        fields: dict = {vol.Required(CONF_CODE): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT))}
        if self.show_advanced_options:
            fields[vol.Optional(CONF_URL, default=DEFAULT_URL)] = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.URL))
        return self.async_show_form(step_id="user", data_schema=vol.Schema(fields), errors=errors)

    # ---------- stap 2: P1 ----------

    async def async_step_p1(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(self.hass, user_input, P1_KEYS, P1_REQUIRED)
            if not errors:
                self._options.update(_picked(user_input, P1_KEYS))
                if self._data.get(CONF_HOME_MODE) == MODE_MEETFASE:
                    self._options.update(dict.fromkeys(PV_KEYS, ""))
                    return await self.async_step_battery()
                return await self.async_step_inverter()
            values = user_input
        else:
            values, self._p1_device = suggest_p1(self.hass)
        return self.async_show_form(
            step_id="p1",
            data_schema=vol.Schema(_entity_schema(P1_KEYS, P1_REQUIRED, values)),
            errors=errors,
            description_placeholders={"home": self._data.get(CONF_HOME_NAME, ""), "device": self._p1_device or "–"},
        )

    # ---------- stap 3: omvormer ----------

    async def async_step_inverter(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(self.hass, user_input, PV_KEYS, PV_REQUIRED)
            if not errors:
                self._options.update(_picked(user_input, PV_KEYS))
                return await self.async_step_battery()
            values = user_input
        else:
            values = suggest_pv(self.hass)
        return self.async_show_form(
            step_id="inverter",
            data_schema=vol.Schema(_entity_schema(PV_KEYS, PV_REQUIRED, values)),
            errors=errors,
        )

    # ---------- stap 4: batterij ----------

    async def async_step_battery(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(self.hass, user_input, BATTERY_KEYS, ())
            if not errors:
                self._options.update(_picked(user_input, BATTERY_KEYS))
                self._options[CONF_BATTERY_INVERT] = bool(user_input.get(CONF_BATTERY_INVERT))
                self._test_error = await self._async_test_send()
                return await (self.async_step_finish() if self._test_error is None else self.async_step_finish_failed())
            values = user_input
        else:
            values = suggest_battery(self.hass)
        return self.async_show_form(step_id="battery", data_schema=_battery_schema(values), errors=errors)

    async def _async_test_send(self) -> str | None:
        """Eén testmeting sturen. Geeft None als het lukte, anders een foutcode."""
        readings = {}
        for key, entity_id in self._options.items():
            if key in SENSOR_KINDS and entity_id and (st := self.hass.states.get(entity_id)):
                readings[key] = (st.state, st.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
        measurement = build_measurement(dt_util.now().replace(microsecond=0), readings, bool(self._options.get(CONF_BATTERY_INVERT)))
        integration_version, ha_version = await _versions(self.hass)
        meters = [name for name, keys in METERS.items() if any(self._options.get(k) for k in keys)]
        api = MijnEnergieApi(async_get_clientsession(self.hass), self._data[CONF_URL], self._data[CONF_TOKEN])
        try:
            await api.send([measurement], {"integration_version": integration_version, "ha_version": ha_version, "queue": 0, "meters": meters})
        except InvalidAuth:
            return "auth"
        except CannotConnect:
            return "connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Onverwachte fout bij testmeting")
            return "unknown"
        return None

    # ---------- afronden ----------

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._create()
        return self.async_show_form(step_id="finish", data_schema=vol.Schema({}),
                                    description_placeholders={"home": self._data[CONF_HOME_NAME]})

    async def async_step_finish_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._create()
        return self.async_show_form(step_id="finish_failed", data_schema=vol.Schema({}),
                                    description_placeholders={"home": self._data[CONF_HOME_NAME]})

    def _create(self) -> ConfigFlowResult:
        return self.async_create_entry(title=self._data[CONF_HOME_NAME], data=self._data, options=self._options)

    # ---------- opnieuw koppelen ----------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._data = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            result, errors = await self._code_form_errors(user_input)
            if result:
                if int(result["home"]["id"]) != int(entry.data.get(CONF_HOME_ID, -1)):
                    return self.async_abort(reason="wrong_home", description_placeholders={"home": entry.title})
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_TOKEN: result["token"],
                        CONF_URL: result["url"],
                        CONF_HOME_NAME: str(result["home"].get("name") or entry.title),
                        CONF_HOME_MODE: str(result["home"].get("mode") or "pv"),
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_CODE): selector.TextSelector()}),
            errors=errors,
            description_placeholders={"home": entry.title},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MijnEnergieOptionsFlow:
        return MijnEnergieOptionsFlow()


class MijnEnergieOptionsFlow(OptionsFlow):
    """Knop 'Configureren': gekozen sensoren wijzigen of opnieuw koppelen."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["p1", "inverter", "battery", "relink"])

    def _save(self, values: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **values})

    async def async_step_p1(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(self.hass, user_input, P1_KEYS, P1_REQUIRED)
            if not errors:
                return self._save(_picked(user_input, P1_KEYS))
        values = user_input or self.config_entry.options
        return self.async_show_form(step_id="p1", data_schema=vol.Schema(_entity_schema(P1_KEYS, P1_REQUIRED, values)), errors=errors)

    async def async_step_inverter(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(self.hass, user_input, PV_KEYS, ())
            if not errors:
                return self._save(_picked(user_input, PV_KEYS))
        values = user_input or self.config_entry.options
        if not any(values.get(k) for k in PV_KEYS):
            values = {**values, **suggest_pv(self.hass)}
        return self.async_show_form(step_id="inverter", data_schema=vol.Schema(_entity_schema(PV_KEYS, (), values)), errors=errors)

    async def async_step_battery(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(self.hass, user_input, BATTERY_KEYS, ())
            if not errors:
                values = _picked(user_input, BATTERY_KEYS)
                values[CONF_BATTERY_INVERT] = bool(user_input.get(CONF_BATTERY_INVERT))
                return self._save(values)
        values = user_input or self.config_entry.options
        return self.async_show_form(step_id="battery", data_schema=_battery_schema(values), errors=errors)

    async def async_step_relink(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self.config_entry.async_start_reauth(self.hass)
        return self.async_abort(reason="relink_started")
