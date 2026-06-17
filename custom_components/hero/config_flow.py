"""Config flow for Hero."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback

from .api import HeroApiError, HeroClient
from .const import CONF_EXCLUDED_WORDS, CONF_HIDE_WEEK_EVENTS, DEFAULT_TENANT_ID, DEFAULT_TIMEZONE, DOMAIN

_LOGGER = logging.getLogger(__name__)


class HeroConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Hero config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            _LOGGER.debug("Starting Hero config flow login")
            entry = _FlowEntry(self.hass, user_input)
            client = HeroClient(self.hass, entry)  # type: ignore[arg-type]
            try:
                data = await client.validate_login(
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                    DEFAULT_TENANT_ID,
                    DEFAULT_TIMEZONE,
                )
            except HeroApiError as err:
                _LOGGER.exception("Hero config flow login failed: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during Hero config flow login")
                errors["base"] = "unknown"
            else:
                _LOGGER.debug(
                    "Hero config flow login succeeded for user_id=%s school_ids=%s",
                    data.get("user_id"),
                    data.get("school_ids"),
                )
                await self.async_set_unique_id(data["user_id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Hero", data=data,
                    options={CONF_EXCLUDED_WORDS: user_input.get(CONF_EXCLUDED_WORDS, "")},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_EXCLUDED_WORDS, default=""): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return HeroOptionsFlow()


class HeroOptionsFlow(config_entries.OptionsFlow):
    """Handle Hero options."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Manage calendar filtering."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_HIDE_WEEK_EVENTS,
                        default=self.config_entry.options.get(CONF_HIDE_WEEK_EVENTS, True),
                    ): bool,
                    vol.Optional(
                        CONF_EXCLUDED_WORDS,
                        default=self.config_entry.options.get(CONF_EXCLUDED_WORDS, ""),
                    ): str,
                }
            ),
        )


class _FlowEntry:
    """Small ConfigEntry stand-in used while validating the login."""

    def __init__(self, hass, data: dict[str, Any]) -> None:
        self.hass = hass
        self.data = data
