# custom_components/heating_analyzer/config_flow.py

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback # callback is not used in this version, can be removed if not planned for options flow
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_GAS_SENSOR,
    CONF_INDOOR_TEMP_SENSOR,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_CLIMATE_ENTITY,
)

class HeatingAnalyzerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Heating Analyzer."""

    VERSION = 1 # Schema version

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            # Basic validation (e.g., check if entities exist) could be added here.
            # For example, by trying to get the state of the entity:
            # if not self.hass.states.get(user_input[CONF_INDOOR_TEMP_SENSOR]):
            #     errors["base"] = "invalid_indoor_temp_sensor" 
            # (You'd need to define these error strings in your translations)

            # For simplicity in this guide, we assume valid entity IDs are entered by the user.
            
            # Optional: If you want to ensure only one instance of this integration can be configured.
            # await self.async_set_unique_id(DOMAIN) 
            # self._abort_if_unique_id_configured()

            return self.async_create_entry(title="Heating Analyzer Settings", data=user_input)

        # Define the schema for the user form using selectors for a better UI experience.
        data_schema = vol.Schema({
            vol.Required(CONF_INDOOR_TEMP_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor"),
            ),
            vol.Required(CONF_OUTDOOR_TEMP_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor"),
            ),
            vol.Required(CONF_CLIMATE_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="climate"),
            ),
            vol.Required(CONF_GAS_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor"),
            ),
        })

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

    # Optional: Implement an options flow if you want to allow users to change settings
    # after the initial setup without removing and re-adding the integration.
    # @staticmethod
    # @callback
    # def async_get_options_flow(config_entry: config_entries.ConfigEntry):
    #     """Get the options flow for this handler."""
    #     return HeatingAnalyzerOptionsFlowHandler(config_entry)

# class HeatingAnalyzerOptionsFlowHandler(config_entries.OptionsFlow):
#     def __init__(self, config_entry: config_entries.ConfigEntry):
#         self.config_entry = config_entry
#
#     async def async_step_init(self, user_input=None):
#         # Manage an options flow for the integration
#         # This would be similar to async_step_user but would modify existing options
#         pass
