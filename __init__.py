# custom_components/heating_analyzer/__init__.py

import logging
from datetime import time, timedelta, datetime
from typing import Optional, Dict, Any

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util
from homeassistant.components.recorder import get_instance as r_get_instance
from homeassistant.components.recorder.history import (
    get_significant_states,
    get_state, # Corrected from get_last_state_changes if only one state is needed
)
from homeassistant.components.climate.const import (
    ATTR_HVAC_ACTION,
    HVAC_ACTION_HEATING,
    HVAC_ACTION_IDLE,
)
from homeassistant.const import ATTR_TEMPERATURE

from .const import (
    DOMAIN,
    CONF_GAS_SENSOR,
    CONF_INDOOR_TEMP_SENSOR,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_CLIMATE_ENTITY,
    UPDATE_HOUR,
    UPDATE_MINUTE,
    SETBACK_START_SEARCH_WINDOW_BEGIN,
    SETBACK_START_SEARCH_WINDOW_END,
    RECOVERY_START_SEARCH_WINDOW_BEGIN,
    RECOVERY_START_SEARCH_WINDOW_END,
    MAX_RECOVERY_SEARCH_END_TIME,
    SIGNIFICANT_SETPOINT_DROP_C,
    SIGNIFICANT_SETPOINT_RISE_C,
    TYPICAL_SETBACK_TEMP_MIN,
    TYPICAL_SETBACK_TEMP_MAX,
    TYPICAL_DAYTIME_TEMP_MIN,
    RECOVERY_TEMP_TOLERANCE_C,
    MIN_IDLE_DURATION_FOR_RECOVERY_END_S,
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
PLATFORMS = ["sensor"]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    # Initialize structure for this config entry
    hass.data[DOMAIN][entry.entry_id] = {"config": entry.data, "sensors": {}}

    # Forward setup to sensor platform
    for platform in PLATFORMS:
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(entry, platform)
        )

    async def scheduled_update_task(now_utc_dt: datetime):
        _LOGGER.info(f"Heating Analyzer ({entry.title}): Starting scheduled update.")
        config_data = hass.data[DOMAIN][entry.entry_id]["config"] # Get from stored config
        try:
            metrics = await async_calculate_all_heating_metrics(
                hass,
                config_data[CONF_INDOOR_TEMP_SENSOR],
                config_data[CONF_OUTDOOR_TEMP_SENSOR],
                config_data[CONF_CLIMATE_ENTITY],
                config_data[CONF_GAS_SENSOR]
            )
            
            # Ensure metrics is a dict, even if calculations fail partially
            if not isinstance(metrics, dict):
                _LOGGER.error(f"Heating Analyzer ({entry.title}): Metrics calculation did not return a dictionary. Skipping update.")
                return

            sensors_dict = hass.data[DOMAIN][entry.entry_id].get('sensors', {})
            if not sensors_dict:
                _LOGGER.warning(f"Heating Analyzer ({entry.title}): Sensor entities not found for update.")
                return

            # Refined sensor update loop
            for metric_key, sensor_instance in sensors_dict.items():
                value_to_update = metrics.get(metric_key) # Returns None if key is not in metrics
                
                # Update sensor only if a new value (even None) is explicitly provided for its metric_key
                # This allows sensors to become 'unknown' if a metric calculation fails for that day
                if metric_key in metrics: # Check if the metric was intended to be calculated
                    _LOGGER.debug(f"Updating sensor {sensor_instance.name} ({metric_key}) with value: {value_to_update}")
                    await sensor_instance.async_update_value(value_to_update)
                # else: # Optional: Log if a sensor exists but its metric_key was not in the results
                #    _LOGGER.debug(f"Metric key {metric_key} not found in calculation results for sensor {sensor_instance.name}.")

            _LOGGER.info(f"Heating Analyzer ({entry.title}): Metrics calculation and sensor updates attempted.")

        except Exception as e:
            _LOGGER.error(f"Heating Analyzer ({entry.title}): Error during scheduled update: {e}", exc_info=True)
        _LOGGER.info(f"Heating Analyzer ({entry.title}): Scheduled update finished.")

    update_time_local = time(UPDATE_HOUR, UPDATE_MINUTE, 0)
    async_track_time_change(hass, scheduled_update_task, hour=update_time_local.hour, minute=update_time_local.minute, second=update_time_local.second)
    _LOGGER.info(f"Heating Analyzer ({entry.title}): Scheduled daily update at {update_time_local}")
    return True


async def _get_historical_states(hass: HomeAssistant, start_utc: datetime, end_utc: datetime, entity_id: str):
    """Helper to fetch significant states for an entity within a period."""
    _LOGGER.debug(f"Fetching history for {entity_id} from {start_utc} to {end_utc}")
    # Ensure start_utc and end_utc are datetime objects
    if not isinstance(start_utc, datetime) or not isinstance(end_utc, datetime):
        _LOGGER.error(f"Invalid datetime objects for history query: start={start_utc}, end={end_utc}")
        return []
        
    history = await r_get_instance(hass).async_add_executor_job(
        get_significant_states, hass, start_utc, end_utc, [entity_id],
        include_start_time_state=True, significant_changes_only=False
    )
    return history.get(entity_id, [])


def parse_time_string(time_str: str) -> time:
    """Parses HH:MM:SS string to time object."""
    return datetime.strptime(time_str, "%H:%M:%S").time()


async def async_calculate_all_heating_metrics(
    hass: HomeAssistant, 
    indoor_temp_id: str, 
    outdoor_temp_id: str, 
    climate_id: str, 
    gas_id: str
) -> Dict[str, Any]:
    """
    Calculates all heating metrics using dynamic time detection.
    Returns a dictionary of calculated metrics.
    """
    _LOGGER.debug(f"Calculating metrics using: Indoor={indoor_temp_id}, Outdoor={outdoor_temp_id}, Climate={climate_id}, Gas={gas_id}")
    
    metrics_results: Dict[str, Any] = { # Initialize all expected keys to None
        METRIC_OPTIMUM_SETPOINT: None,
        METRIC_AVG_OUTDOOR_TEMP_OVERNIGHT: None,
        METRIC_MIN_INDOOR_TEMP_SETBACK: None,
        METRIC_GAS_OVERNIGHT: None,
        METRIC_GAS_RECOVERY: None,
        METRIC_ACTUAL_OVERNIGHT_START_TIME: None,
        METRIC_ACTUAL_RECOVERY_START_TIME: None,
        METRIC_ACTUAL_RECOVERY_END_TIME: None,
        METRIC_OVERNIGHT_SETPOINT_DETECTED: None,
        METRIC_DAYTIME_TARGET_DETECTED: None,
    }
    
    now_local = dt_util.as_local(dt_util.utcnow())
    analysis_day_local = now_local
    
    max_recovery_time_today_local = analysis_day_local.replace(
        hour=parse_time_string(MAX_RECOVERY_SEARCH_END_TIME).hour, 
        minute=parse_time_string(MAX_RECOVERY_SEARCH_END_TIME).minute, 
        second=0, microsecond=0
    )
    if now_local < max_recovery_time_today_local: # Check if current local time is before the max recovery search end time for *today*
        analysis_day_local -= timedelta(days=1)
        _LOGGER.debug(f"Current time is before max recovery search end for today, analyzing previous day's cycle: {analysis_day_local.date()}")

    query_period_start_local = (analysis_day_local - timedelta(days=1)).replace( # Evening of the day *before* the morning recovery we're analyzing
        hour=parse_time_string(SETBACK_START_SEARCH_WINDOW_BEGIN).hour,
        minute=parse_time_string(SETBACK_START_SEARCH_WINDOW_BEGIN).minute,
        second=0, microsecond=0
    )
    query_period_end_local = analysis_day_local.replace( # Morning of the day whose recovery we're analyzing
        hour=parse_time_string(MAX_RECOVERY_SEARCH_END_TIME).hour,
        minute=parse_time_string(MAX_RECOVERY_SEARCH_END_TIME).minute,
        second=0, microsecond=0
    )

    query_start_utc = dt_util.as_utc(query_period_start_local)
    query_end_utc = dt_util.as_utc(query_period_end_local)
    
    _LOGGER.info(f"Broad history query for climate: {query_start_utc} to {query_end_utc} UTC for analysis day {analysis_day_local.date()}")
    climate_history = await _get_historical_states(hass, query_start_utc, query_end_utc, climate_id)
    if not climate_history:
        _LOGGER.warning(f"No climate history found for {climate_id} in the broad query window. Cannot proceed with detailed calculations.")
        return metrics_results # Return default None values

    # --- 1. Detect Actual Overnight Setback Start Time & Setpoint ---
    actual_overnight_start_dt_utc: Optional[datetime] = None
    overnight_setpoint_detected: Optional[float] = None
    previous_setpoint_for_drop_check: Optional[float] = None

    setback_search_start_local = (analysis_day_local - timedelta(days=1)).replace(
        hour=parse_time_string(SETBACK_START_SEARCH_WINDOW_BEGIN).hour,
        minute=parse_time_string(SETBACK_START_SEARCH_WINDOW_BEGIN).minute
    )
    setback_search_end_local = (analysis_day_local - timedelta(days=1)).replace( # Still on the "evening before"
        hour=parse_time_string(SETBACK_START_SEARCH_WINDOW_END).hour,
        minute=parse_time_string(SETBACK_START_SEARCH_WINDOW_END).minute
    )
    if setback_search_end_local.time() < setback_search_start_local.time(): # Handles midnight crossing for window end (e.g. 20:00 to 00:00 next day)
         # If SETBACK_START_SEARCH_WINDOW_END is "00:00:00", it means end of that day.
         # If it's e.g. "01:00:00", it means early morning of 'analysis_day_local'.
         # This logic needs to be careful if window spans midnight.
         # For "20:00" to "00:00", end_local is actually start of 'analysis_day_local'.
         if parse_time_string(SETBACK_START_SEARCH_WINDOW_END) == time(0,0,0):
            setback_search_end_local = analysis_day_local.replace(hour=0, minute=0, second=0, microsecond=0)
         # else if window is e.g. 22:00 to 02:00, end is on analysis_day_local
         elif parse_time_string(SETBACK_START_SEARCH_WINDOW_END) < parse_time_string(SETBACK_START_SEARCH_WINDOW_BEGIN):
            setback_search_end_local = analysis_day_local.replace(
                hour=parse_time_string(SETBACK_START_SEARCH_WINDOW_END).hour,
                minute=parse_time_string(SETBACK_START_SEARCH_WINDOW_END).minute
            )


    _LOGGER.debug(f"Searching for setback start between local {setback_search_start_local} and {setback_search_end_local}")
    
    # Get state just before the search window to establish a baseline setpoint
    initial_climate_state_utc = dt_util.as_utc(setback_search_start_local - timedelta(seconds=1))
    initial_state_obj = await r_get_instance(hass).async_add_executor_job(
        get_state, hass, initial_climate_state_utc, climate_id
    )
    if initial_state_obj and initial_state_obj.attributes.get(ATTR_TEMPERATURE):
        try:
            previous_setpoint_for_drop_check = float(initial_state_obj.attributes[ATTR_TEMPERATURE])
            _LOGGER.debug(f"Initial setpoint before setback search window ({initial_climate_state_utc}): {previous_setpoint_for_drop_check}°C")
        except (ValueError, TypeError):
            _LOGGER.warning(f"Could not parse initial setpoint: {initial_state_obj.attributes[ATTR_TEMPERATURE]}")

    for state in climate_history:
        state_time_utc = state.last_updated
        
        # Check if state is within the specific setback search window
        if not (dt_util.as_utc(setback_search_start_local) <= state_time_utc < dt_util.as_utc(setback_search_end_local)):
            # If state is before our window but after the initial_state_obj time, update previous_setpoint
            if state_time_utc < dt_util.as_utc(setback_search_start_local) and state_time_utc > initial_climate_state_utc and state.attributes.get(ATTR_TEMPERATURE):
                 try: previous_setpoint_for_drop_check = float(state.attributes[ATTR_TEMPERATURE])
                 except (ValueError, TypeError): pass
            continue # Skip states outside the specific search window

        current_setpoint_attr = state.attributes.get(ATTR_TEMPERATURE)
        if current_setpoint_attr is None: continue
        try: current_setpoint = float(current_setpoint_attr)
        except (ValueError, TypeError): continue

        if previous_setpoint_for_drop_check is not None:
            if (previous_setpoint_for_drop_check - current_setpoint >= SIGNIFICANT_SETPOINT_DROP_C and
                TYPICAL_SETBACK_TEMP_MIN <= current_setpoint <= TYPICAL_SETBACK_TEMP_MAX):
                actual_overnight_start_dt_utc = state_time_utc
                overnight_setpoint_detected = current_setpoint
                _LOGGER.info(f"Dynamic Overnight Setback Start DETECTED: {actual_overnight_start_dt_utc} (Local: {dt_util.as_local(actual_overnight_start_dt_utc)}), Setpoint: {overnight_setpoint_detected}°C")
                break 
        previous_setpoint_for_drop_check = current_setpoint
    
    if not actual_overnight_start_dt_utc:
        _LOGGER.warning("Could not dynamically detect overnight setback start time. Further calculations might be unreliable.")
        return metrics_results

    metrics_results[METRIC_ACTUAL_OVERNIGHT_START_TIME] = actual_overnight_start_dt_utc.isoformat()
    metrics_results[METRIC_OVERNIGHT_SETPOINT_DETECTED] = overnight_setpoint_detected

    # --- 2. Detect Actual Morning Recovery Start Time & Daytime Target Setpoint ---
    actual_recovery_start_dt_utc: Optional[datetime] = None
    daytime_target_setpoint: Optional[float] = None
    previous_setpoint_for_rise_check = overnight_setpoint_detected 

    recovery_search_start_local = analysis_day_local.replace(
        hour=parse_time_string(RECOVERY_START_SEARCH_WINDOW_BEGIN).hour,
        minute=parse_time_string(RECOVERY_START_SEARCH_WINDOW_BEGIN).minute
    )
    recovery_search_end_local = analysis_day_local.replace(
        hour=parse_time_string(RECOVERY_START_SEARCH_WINDOW_END).hour,
        minute=parse_time_string(RECOVERY_START_SEARCH_WINDOW_END).minute
    )
    _LOGGER.debug(f"Searching for recovery start between local {recovery_search_start_local} and {recovery_search_end_local}")

    for state in climate_history:
        state_time_utc = state.last_updated
        if state_time_utc < actual_overnight_start_dt_utc: continue # Must be after setback start

        if not (dt_util.as_utc(recovery_search_start_local) <= state_time_utc < dt_util.as_utc(recovery_search_end_local)):
            if state_time_utc < dt_util.as_utc(recovery_search_start_local) and state.attributes.get(ATTR_TEMPERATURE):
                try: previous_setpoint_for_rise_check = float(state.attributes[ATTR_TEMPERATURE])
                except (ValueError, TypeError): pass
            continue

        current_setpoint_attr = state.attributes.get(ATTR_TEMPERATURE)
        if current_setpoint_attr is None: continue
        try: current_setpoint = float(current_setpoint_attr)
        except (ValueError, TypeError): continue
        
        if previous_setpoint_for_rise_check is not None:
            if (current_setpoint - previous_setpoint_for_rise_check >= SIGNIFICANT_SETPOINT_RISE_C and
                current_setpoint >= TYPICAL_DAYTIME_TEMP_MIN):
                actual_recovery_start_dt_utc = state_time_utc
                daytime_target_setpoint = current_setpoint
                _LOGGER.info(f"Dynamic Recovery Start DETECTED: {actual_recovery_start_dt_utc} (Local: {dt_util.as_local(actual_recovery_start_dt_utc)}), Target: {daytime_target_setpoint}°C")
                break
        previous_setpoint_for_rise_check = current_setpoint

    if not actual_recovery_start_dt_utc:
        _LOGGER.warning("Could not dynamically detect morning recovery start time. Further calculations might be unreliable.")
        return metrics_results
    
    metrics_results[METRIC_ACTUAL_RECOVERY_START_TIME] = actual_recovery_start_dt_utc.isoformat()
    metrics_results[METRIC_DAYTIME_TARGET_DETECTED] = daytime_target_setpoint
    actual_overnight_end_dt_utc = actual_recovery_start_dt_utc # Overnight maintenance ends when recovery heating begins

    # --- 3. Detect Actual Morning Recovery End Time ---
    actual_recovery_end_dt_utc: Optional[datetime] = None
    max_recovery_search_end_utc = dt_util.as_utc(analysis_day_local.replace(
        hour=parse_time_string(MAX_RECOVERY_SEARCH_END_TIME).hour,
        minute=parse_time_string(MAX_RECOVERY_SEARCH_END_TIME).minute
    ))
    
    recovery_phase_query_start_utc = actual_recovery_start_dt_utc - timedelta(minutes=1) # Start query just before recovery
    indoor_temp_recovery_history = await _get_historical_states(hass, recovery_phase_query_start_utc, max_recovery_search_end_utc, indoor_temp_id)
    
    # Filter climate_history for the relevant recovery phase
    climate_recovery_phase_history = [s for s in climate_history if actual_recovery_start_dt_utc <= s.last_updated <= max_recovery_search_end_utc]

    last_hvac_action_change_time_utc: Optional[datetime] = None
    last_hvac_action: Optional[str] = None

    # Find the hvac_action at the start of recovery
    for state in climate_recovery_phase_history:
        if state.last_updated >= actual_recovery_start_dt_utc:
            last_hvac_action = state.attributes.get(ATTR_HVAC_ACTION)
            last_hvac_action_change_time_utc = state.last_updated
            break
    
    if daytime_target_setpoint is not None: # Ensure we have a target
        for temp_state in indoor_temp_recovery_history:
            temp_state_time_utc = temp_state.last_updated
            if temp_state_time_utc < actual_recovery_start_dt_utc: continue

            try: current_indoor_temp = float(temp_state.state)
            except (ValueError, TypeError): continue

            # Update current HVAC action based on climate history up to temp_state_time_utc
            for climate_state in climate_recovery_phase_history:
                if climate_state.last_updated <= temp_state_time_utc and climate_state.last_updated >= (last_hvac_action_change_time_utc or actual_recovery_start_dt_utc) :
                    new_action = climate_state.attributes.get(ATTR_HVAC_ACTION)
                    if new_action != last_hvac_action:
                        last_hvac_action = new_action
                        last_hvac_action_change_time_utc = climate_state.last_updated
            
            if current_indoor_temp >= (daytime_target_setpoint - RECOVERY_TEMP_TOLERANCE_C):
                if last_hvac_action != HVAC_ACTION_HEATING and last_hvac_action_change_time_utc is not None:
                    idle_duration_seconds = (temp_state_time_utc - last_hvac_action_change_time_utc).total_seconds()
                    if idle_duration_seconds >= MIN_IDLE_DURATION_FOR_RECOVERY_END_S:
                        actual_recovery_end_dt_utc = temp_state_time_utc 
                        _LOGGER.info(f"Dynamic Recovery End DETECTED: {actual_recovery_end_dt_utc} (Local: {dt_util.as_local(actual_recovery_end_dt_utc)}) based on temp and sustained idle.")
                        break
            if temp_state_time_utc >= max_recovery_search_end_utc: # Ensure we don't go past max search time
                _LOGGER.debug(f"Reached max_recovery_search_end_utc ({max_recovery_search_end_utc}) while searching for recovery end.")
                break
    
    if not actual_recovery_end_dt_utc:
        _LOGGER.warning(f"Could not dynamically detect recovery end time by {max_recovery_search_end_utc}. Using max search time as fallback if system was still heating.")
        # Fallback: if still heating at max_recovery_search_end_utc, use that time.
        # Otherwise, if it went idle before but didn't meet duration, this needs more nuanced fallback.
        # For now, if not found, it remains None, or use max_recovery_search_end_utc as a hard stop.
        final_climate_state_at_max_recovery = None
        for state in reversed(climate_recovery_phase_history):
            if state.last_updated <= max_recovery_search_end_utc:
                final_climate_state_at_max_recovery = state
                break
        if final_climate_state_at_max_recovery and final_climate_state_at_max_recovery.attributes.get(ATTR_HVAC_ACTION) == HVAC_ACTION_HEATING:
             actual_recovery_end_dt_utc = max_recovery_search_end_utc
             _LOGGER.debug(f"Using max_recovery_search_end_utc as recovery end because system was still heating.")
        # If it was idle but didn't meet duration, actual_recovery_end_dt_utc will remain None or its last valid detection.

    metrics_results[METRIC_ACTUAL_RECOVERY_END_TIME] = actual_recovery_end_dt_utc.isoformat() if actual_recovery_end_dt_utc else None
    
    # --- 4. Calculate Metrics using Dynamic Times ---
    if actual_overnight_start_dt_utc and actual_overnight_end_dt_utc:
        outdoor_temp_states_overnight = await _get_historical_states(hass, actual_overnight_start_dt_utc, actual_overnight_end_dt_utc, outdoor_temp_id)
        valid_ot = [float(s.state) for s in outdoor_temp_states_overnight if s.state is not None and s.state not in ("unknown", "unavailable") and isinstance(s.state, (int, float, str)) and s.state.replace('.', '', 1).isdigit()]
        metrics_results[METRIC_AVG_OUTDOOR_TEMP_OVERNIGHT] = round(sum(valid_ot) / len(valid_ot), 1) if valid_ot else None

        indoor_temp_states_overnight = await _get_historical_states(hass, actual_overnight_start_dt_utc, actual_overnight_end_dt_utc, indoor_temp_id)
        valid_it = [float(s.state) for s in indoor_temp_states_overnight if s.state is not None and s.state not in ("unknown", "unavailable") and isinstance(s.state, (int, float, str)) and s.state.replace('.', '', 1).isdigit()]
        metrics_results[METRIC_MIN_INDOOR_TEMP_SETBACK] = round(min(valid_it), 2) if valid_it else None
    
    # --- Gas Consumption Calculation (Placeholder) ---
    # metrics_results[METRIC_GAS_OVERNIGHT] = await _calculate_gas_for_period_utc(hass, gas_id, actual_overnight_start_dt_utc, actual_overnight_end_dt_utc)
    # metrics_results[METRIC_GAS_RECOVERY] = await _calculate_gas_for_period_utc(hass, gas_id, actual_recovery_start_dt_utc, actual_recovery_end_dt_utc)
    _LOGGER.warning("Gas consumption calculation is a placeholder and needs full implementation.")

    # --- 5. Logic for "Optimum Setpoint" (Placeholder) ---
    metrics_results[METRIC_OPTIMUM_SETPOINT] = 16.0 # Placeholder

    _LOGGER.debug(f"Final calculated metrics: {metrics_results}")
    return metrics_results


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


6. sensor.py - Defining Your Sensors (Refined State Class for Gas)
The state_class for gas consumption sensors is changed to SensorStateClass.MEASUREMENT.

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
    METRIC_GAS_OVERNIGHT: {"name": "Gas Used Overnight", "icon": "mdi:fire", "unit": "gas_units", "state_class": SensorStateClass.MEASUREMENT}, # Changed to MEASUREMENT
    METRIC_GAS_RECOVERY: {"name": "Gas Used Recovery", "icon": "mdi:fire-truck", "unit": "gas_units", "state_class": SensorStateClass.MEASUREMENT}, # Changed to MEASUREMENT
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
    # Ensure hass.data structure for this entry's sensors is initialized
    # This was already done in __init__.py's async_setup_entry, but good to be robust.
    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {}).setdefault('sensors', {})
    
    sensors_to_add = []
    # Retrieve the specific sensor dictionary for this config entry
    entry_sensors_dict = hass.data[DOMAIN][entry.entry_id]['sensors']

    for metric_key, meta in SENSOR_TYPES_META.items():
        sensor = HeatingAnalyzerCalculatedSensor(
            hass, # Pass hass
            entry, # Pass config_entry
            unique_id_suffix=metric_key,
            name=meta["name"],
            icon=meta.get("icon"),
            unit_of_measurement=meta.get("unit"),
            device_class=meta.get("device_class"),
            state_class=meta.get("state_class")
        )
        sensors_to_add.append(sensor)
        entry_sensors_dict[metric_key] = sensor

    if sensors_to_add:
        async_add_entities(sensors_to_add, True) # Consider update_before_add behavior
    _LOGGER.info(f"Heating Analyzer ({entry.title}): Sensor platform setup complete with {len(sensors_to_add)} sensors.")


class HeatingAnalyzerCalculatedSensor(SensorEntity):
    _attr_should_poll = False 

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, unique_id_suffix: str, name: str, icon: Optional[str]=None, unit_of_measurement: Optional[str]=None, device_class: Optional[SensorDeviceClass]=None, state_class: Optional[SensorStateClass]=None):
        self._hass = hass # Store HASS instance if needed for other methods
        self._config_entry_id = config_entry.entry_id
        self._attr_name = f"{DEFAULT_NAME} {name}"
        self._attr_unique_id = f"{DOMAIN}_{config_entry.unique_id or config_entry.entry_id}_{unique_id_suffix}"
        
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = unit_of_measurement
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        
        self._attr_native_value = None
        self._attr_extra_state_attributes = {"last_calculated": None, "config_entry_title": config_entry.title}

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._config_entry_id)},
            "name": f"{DEFAULT_NAME} ({self._config_entry_id[:6]})", # Using short part of entry_id for device name
            "manufacturer": "Custom Integration",
            "model": "Heating Analyzer v0.3.1",
            "entry_type": "service", 
        }

    async def async_update_value(self, new_value: Any, new_attributes: Optional[Dict[str, Any]]=None):
        """Update the sensor's state and attributes. Called by __init__.py."""
        if self._attr_device_class == SensorDeviceClass.TIMESTAMP:
            if isinstance(new_value, str):
                try:
                    self._attr_native_value = dt_util.parse_datetime(new_value)
                except ValueError:
                    _LOGGER.error(f"Invalid timestamp string for {self.name}: {new_value}")
                    self._attr_native_value = None
            elif new_value is None:
                self._attr_native_value = None
            # Else, if it's already a datetime object, it would be assigned directly (not typical from JSON-like metrics dict)
            else: # Should be None or ISO string
                 _LOGGER.warning(f"Unexpected value type for TIMESTAMP sensor {self.name}: {type(new_value)}. Setting to None.")
                 self._attr_native_value = None

        else:
            self._attr_native_value = new_value
            
        current_attributes = self._attr_extra_state_attributes or {}
        current_attributes["last_calculated"] = dt_util.now().isoformat()
        if new_attributes: # This argument is not currently used by the calling code in __init__.py
            current_attributes.update(new_attributes)
        self._attr_extra_state_attributes = current_attributes
        
        if self.hass and self.entity_id: # Check if entity is added to HASS
            self.async_write_ha_state()
        elif self.hass: # Entity might not have an ID yet if called before full setup
             _LOGGER.debug(f"Sensor {self.name} has HASS but no entity_id yet, scheduling update.")
             self.async_schedule_update_ha_state(True)
