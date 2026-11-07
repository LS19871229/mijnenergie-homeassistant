"""Koppelschermen en Configureren.

Koppelen:   koppelcode -> (sensorkeuze uit het portaal overnemen) of installatie -> P1 -> zonnepanelen -> batterij -> huis -> klaar
Configureren: installatie en sensoren (zelfde stappen) of opnieuw koppelen.
De stappen voor de sensorkeuze staan in SensorStepsMixin en worden door beide flows gebruikt.
"""

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
    CONF_AUTO_UPDATE,
    CONF_CODE,
    CONF_CONFIG,
    CONF_CONFIG_DIRTY,
    CONF_CONFIG_REV,
    CONF_HOME_ID,
    CONF_HOME_MODE,
    CONF_HOME_NAME,
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_URL,
    DOMAIN,
    INST_INVERTER,
    INST_MULTI,
    INST_NONE,
    INSTALLATIONS,
    MAX_PV_IN_FLOW,
    MODE_MEETFASE,
    WITH_BATTERY,
)
from .detect import default_invert, suggest_battery, suggest_house, suggest_p1, suggest_pv
from .measure import collect_raw, config_entities, config_meters, empty_config, normalize_config, unit_supported

_LOGGER = logging.getLogger(__name__)

SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
TEXT = selector.TextSelector()
CAPACITY = selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=999, step=0.1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="kWh"))


async def _versions(hass: HomeAssistant) -> tuple[str, str]:
    integration = await async_get_integration(hass, DOMAIN)
    return str(integration.version or "0"), HA_VERSION


def _clean_code(code: str) -> str:
    return "".join(ch for ch in str(code) if ch.isdigit())


def _field(key: str, required: bool, current: Any) -> vol.Marker:
    marker = vol.Required if required else vol.Optional
    return marker(key, description={"suggested_value": current} if current not in (None, "") else None)


def _check(hass: HomeAssistant, value: Any, kind: str) -> str | None:
    """Foutcode voor één gekozen sensor, of None als die bruikbaar is."""
    state = hass.states.get(value)
    if state is None:
        return "entity_not_found"
    if not unit_supported(kind, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)):
        return f"wrong_unit_{kind}"
    if kind in ("energy", "gas") and state.attributes.get("state_class") == "measurement":
        return "not_a_total"
    return None


def _validate(hass: HomeAssistant, user_input: Mapping[str, Any], specs: list[tuple[str, str, bool]]) -> dict[str, str]:
    """specs: (formulierveld, soort, verplicht)."""
    errors: dict[str, str] = {}
    for key, kind, required in specs:
        value = user_input.get(key)
        if not value:
            if required:
                errors[key] = "required"
            continue
        if (err := _check(hass, value, kind)) is not None:
            errors[key] = err
    return errors


def _test_measurement(hass: HomeAssistant, cfg: dict[str, Any]) -> dict[str, Any]:
    """Testmeting: ruwe waarden (het portaal rekent)."""
    def get_state(entity_id: str) -> tuple[Any, str | None] | None:
        st = hass.states.get(entity_id)
        return None if st is None else (st.state, st.attributes.get(ATTR_UNIT_OF_MEASUREMENT))

    return {"ts": dt_util.now().replace(microsecond=0).isoformat(timespec="seconds"), "raw": collect_raw(cfg, get_state)}


class SensorStepsMixin:
    """Stappen voor de sensorkeuze. Verwacht self._cfg (configuratie) en _async_sensors_done()."""

    hass: HomeAssistant
    _cfg: dict[str, Any]
    _first_time: bool = False  # bij koppelen: voorstellen invullen

    async def _async_sensors_done(self) -> ConfigFlowResult:
        raise NotImplementedError

    # ---------- installatie ----------

    async def async_step_installation(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            inst = user_input.get("installation")
            self._cfg["installation"] = inst if inst in INSTALLATIONS else INST_INVERTER
            return await self.async_step_p1()
        sel = selector.SelectSelector(selector.SelectSelectorConfig(
            options=INSTALLATIONS, translation_key="installation", mode=selector.SelectSelectorMode.LIST))
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="installation",
            data_schema=vol.Schema({vol.Required("installation", default=self._cfg["installation"]): sel}),
        )

    # ---------- P1 ----------

    async def async_step_p1(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        p1 = self._cfg["p1"]
        device = None
        if user_input is not None:
            errors = _validate(self.hass, user_input, [("p1_power", "power", True), ("p1_import", "energy", True),
                                                       ("p1_export", "energy", True), ("gas", "gas", False)])
            if user_input.get("p1_import") and user_input.get("p1_import") == user_input.get("p1_export"):
                errors["p1_export"] = "same_entity"
            values = user_input
            if not errors:
                self._cfg["p1"] = {"power": user_input["p1_power"], "import": user_input["p1_import"],
                                   "export": user_input["p1_export"], "gas": user_input.get("gas") or "",
                                   "invert": bool(user_input.get("p1_invert"))}
                return await self._after_p1()
        else:
            values = {"p1_power": p1["power"], "p1_import": p1["import"], "p1_export": p1["export"], "gas": p1["gas"],
                      "p1_invert": p1.get("invert", False)}
            if self._first_time or not p1["power"]:
                sugg, device = suggest_p1(self.hass)
                values = {"p1_power": sugg.get("power"), "p1_import": sugg.get("import"),
                          "p1_export": sugg.get("export"), "gas": sugg.get("gas"),
                          "p1_invert": default_invert(self.hass, sugg.get("power"))}
        schema = vol.Schema({
            _field("p1_power", True, values.get("p1_power")): SENSOR,
            vol.Optional("p1_invert", default=bool(values.get("p1_invert"))): selector.BooleanSelector(),
            _field("p1_import", True, values.get("p1_import")): SENSOR,
            _field("p1_export", True, values.get("p1_export")): SENSOR,
            _field("gas", False, values.get("gas")): SENSOR,
        })
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="p1", data_schema=schema, errors=errors,
            description_placeholders={"device": device or "–", "home": getattr(self, "_home_name", "")},
        )

    async def _after_p1(self) -> ConfigFlowResult:
        inst = self._cfg["installation"]
        if inst == INST_NONE:
            self._cfg["pv"], self._cfg["battery"], self._cfg["house"] = [], [], {"power": ""}
            return await self._async_sensors_done()
        return await self.async_step_pv()

    # ---------- zonnepanelen ----------

    def _pv_count(self) -> int:
        return MAX_PV_IN_FLOW if self._cfg["installation"] == INST_MULTI else 1

    async def async_step_pv(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        n = self._pv_count()
        hybrid = self._cfg["installation"] in WITH_BATTERY
        if user_input is not None:
            specs = []
            for i in range(1, n + 1):
                specs += [(f"pv_power_{i}", "power", i == 1), (f"pv_energy_{i}", "energy", i == 1)]
            errors = _validate(self.hass, user_input, specs)
            seen: dict[str, str] = {}
            for i in range(1, n + 1):
                p, e = user_input.get(f"pv_power_{i}"), user_input.get(f"pv_energy_{i}")
                if bool(p) != bool(e) and i > 1:
                    errors[f"pv_energy_{i}" if p else f"pv_power_{i}"] = "required"
                for key, val in ((f"pv_power_{i}", p), (f"pv_energy_{i}", e)):
                    if val and val in seen:
                        errors[key] = "duplicate"
                    if val:
                        seen[val] = key
            values = user_input
            if not errors:
                self._cfg["pv"] = [
                    {"label": str(user_input.get(f"pv_label_{i}") or ""), "power": user_input[f"pv_power_{i}"],
                     "energy": user_input[f"pv_energy_{i}"]}
                    for i in range(1, n + 1) if user_input.get(f"pv_power_{i}") and user_input.get(f"pv_energy_{i}")
                ]
                return await self._after_pv()
        else:
            values = {}
            for i, src in enumerate(self._cfg["pv"][:n], start=1):
                values.update({f"pv_label_{i}": src["label"], f"pv_power_{i}": src["power"], f"pv_energy_{i}": src["energy"]})
            if not self._cfg["pv"]:
                sugg = suggest_pv(self.hass, hybrid=hybrid)
                values.update({"pv_power_1": sugg.get("power"), "pv_energy_1": sugg.get("energy")})
        fields: dict = {}
        for i in range(1, n + 1):
            if n > 1:
                fields[_field(f"pv_label_{i}", False, values.get(f"pv_label_{i}"))] = TEXT
            fields[_field(f"pv_power_{i}", i == 1, values.get(f"pv_power_{i}"))] = SENSOR
            fields[_field(f"pv_energy_{i}", i == 1, values.get(f"pv_energy_{i}"))] = SENSOR
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="pv_multi" if n > 1 else ("pv_hybrid" if hybrid else "pv"),
            data_schema=vol.Schema(fields), errors=errors,
        )

    # Zelfde stap, andere uitleg (step_id bepaalt de tekst)
    async def async_step_pv_multi(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_pv(user_input)

    async def async_step_pv_hybrid(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_pv(user_input)

    async def _after_pv(self) -> ConfigFlowResult:
        if self._cfg["installation"] in WITH_BATTERY or self._cfg["installation"] == INST_MULTI:
            return await self.async_step_battery()
        self._cfg["battery"] = []
        return await self.async_step_house()

    # ---------- batterij ----------

    async def async_step_battery(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        required = self._cfg["installation"] in WITH_BATTERY
        if user_input is not None:
            errors = _validate(self.hass, user_input, [
                ("battery_power", "power", required), ("battery_soc", "percent", required),
                ("battery_charge", "energy", False), ("battery_discharge", "energy", False)])
            if bool(user_input.get("battery_charge")) != bool(user_input.get("battery_discharge")):
                errors["battery_discharge"] = "both_counters"
            if not required and bool(user_input.get("battery_power")) != bool(user_input.get("battery_soc")):
                errors["battery_soc"] = "required"
            values = user_input
            if not errors:
                if user_input.get("battery_power"):
                    cap = user_input.get("battery_capacity")
                    self._cfg["battery"] = [{
                        "label": "", "power": user_input["battery_power"], "soc": user_input["battery_soc"],
                        "charge": user_input.get("battery_charge") or "", "discharge": user_input.get("battery_discharge") or "",
                        "invert": bool(user_input.get("battery_invert")),
                        "capacity": float(cap) if cap else None,
                    }]
                    # Extra batterijen (alleen via het portaal in te stellen) blijven staan
                    self._cfg["battery"] += self._extra_batteries
                else:
                    self._cfg["battery"] = []
                return await self.async_step_house()
        else:
            cur = self._cfg["battery"][0] if self._cfg["battery"] else None
            if cur:
                values = {"battery_power": cur["power"], "battery_soc": cur["soc"], "battery_charge": cur["charge"],
                          "battery_discharge": cur["discharge"], "battery_invert": cur["invert"], "battery_capacity": cur["capacity"]}
            else:
                sugg = suggest_battery(self.hass)
                values = {f"battery_{k}": v for k, v in sugg.items()}
                values["battery_invert"] = default_invert(self.hass, sugg.get("power"))
        self._extra_batteries = self._cfg["battery"][1:] if self._cfg["battery"] else []
        schema = vol.Schema({
            _field("battery_power", required, values.get("battery_power")): SENSOR,
            vol.Optional("battery_invert", default=bool(values.get("battery_invert"))): selector.BooleanSelector(),
            _field("battery_soc", required, values.get("battery_soc")): SENSOR,
            _field("battery_charge", False, values.get("battery_charge")): SENSOR,
            _field("battery_discharge", False, values.get("battery_discharge")): SENSOR,
            _field("battery_capacity", False, values.get("battery_capacity")): CAPACITY,
        })
        return self.async_show_form(step_id="battery", data_schema=schema, errors=errors)  # type: ignore[attr-defined]

    _extra_batteries: list[dict[str, Any]] = []

    # ---------- huis ----------

    async def async_step_house(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(self.hass, user_input, [("house_power", "power", False)])
            if not errors:
                self._cfg["house"] = {"power": user_input.get("house_power") or ""}
                return await self._async_sensors_done()
            current = user_input.get("house_power")
        else:
            current = self._cfg["house"]["power"] or (suggest_house(self.hass) if self._first_time else None)
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="house", data_schema=vol.Schema({_field("house_power", False, current): SENSOR}), errors=errors)


class MijnEnergieConfigFlow(SensorStepsMixin, ConfigFlow, domain=DOMAIN):
    """Koppelen met een koppelcode, daarna de sensorkeuze."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._cfg = empty_config()
        self._rev = 0
        self._first_time = True
        self._home_name = ""
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
                self._home_name = str(home.get("name") or f"Woning {home['id']}")
                self._data = {
                    CONF_URL: result["url"], CONF_TOKEN: result["token"], CONF_HOME_ID: int(home["id"]),
                    CONF_HOME_NAME: self._home_name, CONF_HOME_MODE: str(home.get("mode") or "pv"),
                }
                await self.async_set_unique_id(f"home_{home['id']}")
                self._abort_if_unique_id_configured(updates={CONF_TOKEN: result["token"], CONF_URL: result["url"]})
                portal_cfg = normalize_config(result.get("config"))
                if int(result.get("config_rev") or 0) > 0 and config_entities(portal_cfg):
                    # Sensorkeuze bestaat al in het portaal: overnemen
                    self._cfg, self._rev = portal_cfg, int(result["config_rev"])
                    self._test_error = await self._async_test_send(push=False)
                    return await self.async_step_finish_portal()
                if self._data[CONF_HOME_MODE] == MODE_MEETFASE:
                    self._cfg["installation"] = INST_NONE
                return await self.async_step_installation()

        fields: dict = {vol.Required(CONF_CODE): selector.TextSelector()}
        if self.show_advanced_options:
            fields[vol.Optional(CONF_URL, default=DEFAULT_URL)] = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.URL))
        return self.async_show_form(step_id="user", data_schema=vol.Schema(fields), errors=errors)

    # ---------- afronden ----------

    async def _async_sensors_done(self) -> ConfigFlowResult:
        self._test_error = await self._async_test_send(push=True)
        return await (self.async_step_finish() if self._test_error is None else self.async_step_finish_failed())

    async def _async_test_send(self, push: bool) -> str | None:
        """Testmeting sturen en (bij een nieuwe keuze) de sensorkeuze naar het portaal. None = gelukt."""
        integration_version, ha_version = await _versions(self.hass)
        api = MijnEnergieApi(async_get_clientsession(self.hass), self._data[CONF_URL], self._data[CONF_TOKEN])
        try:
            await api.send([_test_measurement(self.hass, self._cfg)], {
                "integration_version": integration_version, "ha_version": ha_version, "queue": 0,
                "meters": config_meters(self._cfg), "config_rev": self._rev})
            if push:
                self._rev = await api.push_config(self._cfg)
        except InvalidAuth:
            return "auth"
        except CannotConnect:
            return "connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Onverwachte fout bij testmeting")
            return "unknown"
        return None

    def _create(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self._data[CONF_HOME_NAME], data=self._data,
            options={CONF_CONFIG: normalize_config(self._cfg), CONF_CONFIG_REV: self._rev,
                     CONF_CONFIG_DIRTY: self._rev == 0 and bool(config_entities(self._cfg))},
        )

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._create()
        return self.async_show_form(step_id="finish", data_schema=vol.Schema({}), description_placeholders={"home": self._home_name})

    async def async_step_finish_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._create()
        return self.async_show_form(step_id="finish_failed", data_schema=vol.Schema({}), description_placeholders={"home": self._home_name})

    async def async_step_finish_portal(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._create()
        return self.async_show_form(step_id="finish_portal", data_schema=vol.Schema({}),
                                    description_placeholders={"home": self._home_name, "rev": str(self._rev)})

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
                        CONF_TOKEN: result["token"], CONF_URL: result["url"],
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


class MijnEnergieOptionsFlow(SensorStepsMixin, OptionsFlow):
    """Knop 'Configureren': installatie en sensoren wijzigen, of opnieuw koppelen."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._cfg = normalize_config(self.config_entry.options.get(CONF_CONFIG))
        self._first_time = False
        return self.async_show_menu(
            step_id="init", menu_options=["installation", "auto_update", "relink"],
            description_placeholders={"rev": str(self.config_entry.options.get(CONF_CONFIG_REV) or 0)},
        )

    async def _async_sensors_done(self) -> ConfigFlowResult:
        # Wordt binnen een minuut naar het portaal gestuurd (CONF_CONFIG_DIRTY)
        return self.async_create_entry(data={**self.config_entry.options, CONF_CONFIG: normalize_config(self._cfg), CONF_CONFIG_DIRTY: True})

    async def async_step_auto_update(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options, CONF_AUTO_UPDATE: bool(user_input.get(CONF_AUTO_UPDATE))})
        return self.async_show_form(step_id="auto_update", data_schema=vol.Schema({
            vol.Required(CONF_AUTO_UPDATE, default=bool(self.config_entry.options.get(CONF_AUTO_UPDATE, True))): selector.BooleanSelector()}))

    async def async_step_relink(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self.config_entry.async_start_reauth(self.hass)
        return self.async_abort(reason="relink_started")
