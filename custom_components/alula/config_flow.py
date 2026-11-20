"""Config flow for the Alula integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithConfigEntry,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback

from .const import (
    CONF_NIGHT_ARM_VARIANT,
    CONF_PANEL_ID,
    DEFAULT_NIGHT_ARM_VARIANT,
    DOMAIN,
    FORCE_ARM_VARIANTS,
)
from .pyalula import AlulaClient
from .pyalula.client import AlulaApiError, AlulaAuthError

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_PANEL_ID, default=""): str,
    }
)


class AlulaOptionsFlow(OptionsFlowWithConfigEntry):
    """Choose the Arm Night payload. No PIN is stored here."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = self.options.get(CONF_NIGHT_ARM_VARIANT, DEFAULT_NIGHT_ARM_VARIANT)
        if current not in FORCE_ARM_VARIANTS:
            current = DEFAULT_NIGHT_ARM_VARIANT
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NIGHT_ARM_VARIANT, default=current): vol.In(
                        FORCE_ARM_VARIANTS
                    ),
                }
            ),
        )


class AlulaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup UI for Alula."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AlulaOptionsFlow:
        """Options for Arm Night. The flow does not ask for or store a PIN."""
        return AlulaOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            panel_id = user_input.get(CONF_PANEL_ID) or None

            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()

            client = AlulaClient()
            try:
                await client.login(username, password)
            except AlulaAuthError:
                errors["base"] = "invalid_auth"
            except AlulaApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during Alula login")
                errors["base"] = "unknown"
            finally:
                await client.close()

            if not errors:
                data = {
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                }
                if panel_id:
                    data[CONF_PANEL_ID] = panel_id
                return self.async_create_entry(title=f"Alula ({username})", data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )
