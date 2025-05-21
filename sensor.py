# custom_components/heating_analyzer/sensor.py
import logging
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN, 
    DEFAULT_NAME,
    METRIC_OPTIMUM_SETPOINT,
    METRIC_AVG_OUTDOOR_TEMP_OVERNIGHT,
    METRIC_MIN_INDOOR_TEMP_SETBACK,
    METRIC_GAS_OVERNIGHT,
    METRIC_GAS_RECOVERY,
    METRIC_ACTUAL_OVERNIGHT_START_TIME,
    METRIC_ACTUAL_RECOVERY_START_TIME,
    METRIC_ACTUAL_RECOVERY_END_TIME,
    METRIC_OVERNIGHT_SETPOINT_DETECTED,
    METRIC_DAYTIME_TARGET_DETECTED,
)

_LOGGER = logging.getLogger(__name__)

SENSOR_TYPES_META = {
    METRIC_OPTIMUM_SETPOINT: {"name": "Optimum Overnight Setpoint", "icon": "mdi:thermometer-auto", "unit": "°C", "device_class": SensorDeviceClass.TEMPERATURE, "state_class": SensorStateClass.MEASUREMENT},
    METRIC_AVG_OUTDOOR_TEMP_OVERNIGHT: {"name": "Avg Outdoor Temp Overnight", "icon": "mdi:thermometer-lines", "unit": "°C", "device_class": SensorDeviceClass.TEMPERATURE, "state_class": SensorStateClass.MEASUREMENT},
    METRIC_MIN_INDOOR_TEMP_SETBACK: {"name": "Min Indoor Temp During Setback", "icon": "mdi:thermometer-low", "unit": "°C", "device_class": SensorDeviceClass.TEMPERATURE, "state_class": SensorStateClass.MEASUREMENT},
    METRIC_GAS_OVERNIGHT: {"name": "Gas Used Overnight", "icon": "mdi:fire", "unit": "gas_units", "state_class": SensorStateClass.MEASUREMENT},
    METRIC_GAS_RECOVERY: {"name": "Gas Used Recovery", "icon": "mdi:fire-truck", "unit": "gas_units", "state_class": SensorStateClass.MEASUREMENT},
    METRIC_ACTUAL_OVERNIGHT_START_TIME: {"name": "Actual Overnight Start", "icon": "mdi:clock-start", "device_class": SensorDeviceClass.TIMESTAMP},
    METRIC_ACTUAL_RECOVERY_START_TIME: {"name": "Actual Recovery Start", "icon": "mdi:clock-play", "device_class": SensorDeviceClass.TIMESTAMP},
    METRIC_ACTUAL_RECOVERY_END_TIME: {"name": "Actual Recovery End", "icon": "mdi:clock-end", "device_class": SensorDeviceClass.TIMESTAMP},
    METRIC_OVERNIGHT_SETPOINT_DETECTED: {"name": "Detected Overnight Setpoint", "icon": "mdi:thermostat-box", "unit": "°C", "device_class": SensorDeviceClass.TEMPERATURE, "state_class": SensorStateClass.MEASUREMENT},
    METRIC_DAYTIME_TARGET_DETECTED: {"name": "Detected Daytime Target", "icon": "mdi:thermostat", "unit": "°C", "device_class": SensorDeviceClass.TEMPERATURE, "state_class": SensorStateClass.MEASUREMENT},
}

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Heating Analyzer sensors from a config entry."""
    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {}).setdefault('sensors', {})
    
    sensors_to_add = []
    entry_sensors_dict = hass.data[DOMAIN][entry.entry_id]['sensors']

    for metric_key, meta in SENSOR_TYPES_META.items():
        sensor = HeatingAnalyzerCalculatedSensor(
            hass,
            entry,
            metric_key=metric_key, 
            name=meta["name"],
            icon=meta.get("icon"),
            unit_of_measurement=meta.get("unit"),
            device_class=meta.get("device_class"),
            state_class=meta.get("state_class")
        )
        sensors_to_add.append(sensor)
        entry_sensors_dict[metric_key] = sensor

    if sensors_to_add:
        async_add_entities(sensors_to_add, True)
    _LOGGER.info(f"Heating Analyzer ({entry.title}): Sensor platform setup complete with {len(sensors_to_add)} sensors.")


class HeatingAnalyzerCalculatedSensor(SensorEntity):
    _attr_should_poll = False 

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, metric_key: str, name: str, icon: Optional[str]=None, unit_of_measurement: Optional[str]=None, device_class: Optional[SensorDeviceClass]=None, state_class: Optional[SensorStateClass]=None):
        self._hass = hass
        self._config_entry_id = config_entry.entry_id
        self._metric_key = metric_key
        
        self._attr_name = f"{DEFAULT_NAME} {name}"
        self._attr_unique_id = f"{DOMAIN}_{config_entry.entry_id}_{metric_key}"
        
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = unit_of_measurement
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        
        self._attr_native_value = None
        self._attr_extra_state_attributes = {"last_calculated": None, "config_entry_title": config_entry.title, "metric_key": metric_key}

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._config_entry_id)},
            "name": f"{DEFAULT_NAME} ({self._config_entry_id[:6]})",
            "manufacturer": "Custom Integration",
            "model": "Heating Analyzer v0.5.0", # Update version
            "entry_type": "service", 
        }

    async def async_update_value(self, new_value: Any, new_attributes: Optional[Dict[str, Any]]=None):
        if self._attr_device_class == SensorDeviceClass.TIMESTAMP:
            if isinstance(new_value, str):
                try: self._attr_native_value = dt_util.parse_datetime(new_value)
                except ValueError: self._attr_native_value = None
            elif new_value is None: self._attr_native_value = None
            else: self._attr_native_value = None
        else:
            self._attr_native_value = new_value
            
        current_attributes = self._attr_extra_state_attributes or {}
        current_attributes["last_calculated"] = dt_util.now().isoformat()
        self._attr_extra_state_attributes = current_attributes
        
        if self.hass and self.entity_id:
            self.async_write_ha_state()
        elif self.hass:
             self.async_schedule_update_ha_state(True)
