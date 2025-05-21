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
    get_state,
    state_changes_during_period, # Added for gas calculation
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
    hass.data[DOMAIN][entry.entry_id] = {"config": entry.data, "sensors": {}}

    for platform in PLATFORMS:
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(entry, platform)
        )

    async def scheduled_update_task(now_utc_dt: datetime):
        _LOGGER.info(f"Heating Analyzer ({entry.title}): Starting scheduled update.")
        config_data = hass.data[DOMAIN][entry.entry_id]["config"]
        try:
            metrics = await async_calculate_all_heating_metrics(
                hass,
                config_data[CONF_INDOOR_TEMP_SENSOR],
                config_data[CONF_OUTDOOR_TEMP_SENSOR],
                config_data[CONF_CLIMATE_ENTITY],
                config_data[CONF_GAS_SENSOR]
            )
            
            if not isinstance(metrics, dict):
                _LOGGER.error(f"Heating Analyzer ({entry.title}): Metrics calculation did not return a dictionary. Skipping update.")
                return

            sensors_dict = hass.data[DOMAIN][entry.entry_id].get('sensors', {})
            if not sensors_dict:
                _LOGGER.warning(f"Heating Analyzer ({entry.title}): Sensor entities not found for update.")
                return

            for metric_key, sensor_instance in sensors_dict.items():
                value_to_update = metrics.get(metric_key)
                if metric_key in metrics:
                    _LOGGER.debug(f"Updating sensor {sensor_instance.name} ({metric_key}) with value: {value_to_update}")
                    await sensor_instance.async_update_value(value_to_update)

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


async def _calculate_gas_for_period_utc(
    hass: HomeAssistant, 
    gas_entity_id: str, 
    period_start_utc: Optional[datetime], 
    period_end_utc: Optional[datetime]
) -> Optional[float]:
    """
    Calculates gas consumption for a given period, accounting for a daily resetting sensor.
    Assumes the sensor resets to 0, and a drop in value indicates a reset.
    """
    if not gas_entity_id or not period_start_utc or not period_end_utc:
        _LOGGER.debug("Gas calculation skipped: Missing entity ID or period boundaries.")
        return None
    
    if period_start_utc >= period_end_utc:
        _LOGGER.debug(f"Gas calculation skipped: Start time {period_start_utc} is not before end time {period_end_utc}.")
        return 0.0 # Or None, depending on how you want to handle invalid periods

    _LOGGER.debug(f"Calculating gas for {gas_entity_id} from {period_start_utc} to {period_end_utc}")

    try:
        history_data = await r_get_instance(hass).async_add_executor_job(
            state_changes_during_period,
            hass,
            period_start_utc,
            period_end_utc,
            gas_entity_id,
            include_start_time_state=True,
            no_attributes=True  # We only need the state value
        )
    except Exception as e:
        _LOGGER.error(f"Error fetching history for gas sensor {gas_entity_id}: {e}")
        return None

    states = history_data.get(gas_entity_id, [])

    if not states:
        _LOGGER.warning(f"No gas history found for {gas_entity_id} in period {period_start_utc} to {period_end_utc}.")
        # Attempt to get single state at start and end if no changes occurred
        start_state_obj = await r_get_instance(hass).async_add_executor_job(get_state, hass, period_start_utc, gas_entity_id)
        end_state_obj = await r_get_instance(hass).async_add_executor_job(get_state, hass, period_end_utc, gas_entity_id)
        if start_state_obj and end_state_obj:
            try:
                start_val = float(start_state_obj.state)
                end_val = float(end_state_obj.state)
                if end_val >= start_val: # No reset presumed if no intermediate states and end >= start
                    return round(end_val - start_val, 3)
                else: # End value is less than start, implies a reset occurred but we have no intermediate states
                    _LOGGER.warning(f"Gas sensor {gas_entity_id} shows end value < start value with no intermediate states. Assuming reset. Consumption calculated as end_val + (value_before_reset - start_val). This part is ambiguous without more data.")
                    # This scenario is hard to resolve without knowing the value before reset.
                    # A safe assumption might be to return end_val if it's small (assuming it's post-reset)
                    # and log that the pre-reset part couldn't be determined.
                    # For now, returning None as it's ambiguous.
                    return None
            except (ValueError, TypeError):
                _LOGGER.error(f"Could not parse gas state values for {gas_entity_id} when handling no history changes.")
                return None
        return None


    total_consumption = 0.0
    
    try:
        # Get the state at the very beginning of the period (or first available after)
        # `states[0]` should be this due to `include_start_time_state=True`
        previous_value = float(states[0].state)
    except (ValueError, TypeError, IndexError):
        _LOGGER.error(f"Could not get initial gas state or parse it for {gas_entity_id} at {period_start_utc}.")
        return None # Cannot proceed without a valid starting value

    _LOGGER.debug(f"Gas calc: Initial value for {gas_entity_id} at/after {states[0].last_updated} = {previous_value}")

    for i in range(1, len(states)):
        current_state_obj = states[i]
        try:
            current_value = float(current_state_obj.state)
        except (ValueError, TypeError):
            _LOGGER.warning(f"Could not parse gas state {current_state_obj.state} for {gas_entity_id} at {current_state_obj.last_updated}. Skipping this state.")
            continue # Skip this problematic state

        if current_value < previous_value:  # Reset detected
            _LOGGER.debug(f"Gas reset detected for {gas_entity_id}: {previous_value} -> {current_value} at {current_state_obj.last_updated}")
            total_consumption += previous_value  # Add the amount used up to the reset
        
        previous_value = current_value

    # Add the amount from the last segment (after the last reset, or total if no resets within the fetched states for the period)
    total_consumption += previous_value
    
    _LOGGER.info(f"Calculated gas consumption for {gas_entity_id} from {period_start_utc} to {period_end_utc}: {round(total_consumption, 3)}")
    return round(total_consumption, 3)


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
    
    metrics_results: Dict[str, Any] = {
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
    if now_local < max_recovery_time_today_local:
        analysis_day_local -= timedelta(days=1)
        _LOGGER.debug(f"Current time is before max recovery search end for today, analyzing previous day's cycle: {analysis_day_local.date()}")

    query_period_start_local = (analysis_day_local - timedelta(days=1)).replace(
        hour=parse_time_string(SETBACK_START_SEARCH_WINDOW_BEGIN).hour,
        minute=parse_time_string(SETBACK_START_SEARCH_WINDOW_BEGIN).minute,
        second=0, microsecond=0
    )
    query_period_end_local = analysis_day_local.replace(
        hour=parse_time_string(MAX_RECOVERY_SEARCH_END_TIME).hour,
        minute=parse_time_string(MAX_RECOVERY_SEARCH_END_TIME).minute,
        second=0, microsecond=0
    )

    query_start_utc = dt_util.as_utc(query_period_start_local)
    query_end_utc = dt_util.as_utc(query_period_end_local)
    
    _LOGGER.info(f"Broad history query for climate: {query_start_utc} to {query_end_utc} UTC for analysis day {analysis_day_local.date()}")
    climate_history = await _get_historical_states(hass, query_start_utc, query_end_utc, climate_id)
    if not climate_history:
        _LOGGER.warning(f"No climate history found for {climate_id}. Cannot proceed.")
        return metrics_results

    # --- 1. Detect Actual Overnight Setback Start Time & Setpoint ---
    actual_overnight_start_dt_utc: Optional[datetime] = None
    overnight_setpoint_detected: Optional[float] = None
    previous_setpoint_for_drop_check: Optional[float] = None

    setback_search_start_local = (analysis_day_local - timedelta(days=1)).replace(
        hour=parse_time_string(SETBACK_START_SEARCH_WINDOW_BEGIN).hour,
        minute=parse_time_string(SETBACK_START_SEARCH_WINDOW_BEGIN).minute
    )
    # Determine end of setback search window (can cross midnight)
    setback_search_end_time_obj = parse_time_string(SETBACK_START_SEARCH_WINDOW_END)
    if setback_search_end_time_obj < parse_time_string(SETBACK_START_SEARCH_WINDOW_BEGIN): # Crosses midnight
        setback_search_end_local = analysis_day_local.replace(
            hour=setback_search_end_time_obj.hour, minute=setback_search_end_time_obj.minute
        )
    else: # Same day
        setback_search_end_local = (analysis_day_local - timedelta(days=1)).replace(
            hour=setback_search_end_time_obj.hour, minute=setback_search_end_time_obj.minute
        )
    
    _LOGGER.debug(f"Searching for setback start between local {setback_search_start_local} and {setback_search_end_local}")
    
    initial_climate_state_utc = dt_util.as_utc(setback_search_start_local - timedelta(seconds=1))
    initial_state_obj = await r_get_instance(hass).async_add_executor_job(
        get_state, hass, initial_climate_state_utc, climate_id
    )
    if initial_state_obj and initial_state_obj.attributes.get(ATTR_TEMPERATURE):
        try:
            previous_setpoint_for_drop_check = float(initial_state_obj.attributes[ATTR_TEMPERATURE])
        except (ValueError, TypeError):
            _LOGGER.warning(f"Could not parse initial setpoint: {initial_state_obj.attributes[ATTR_TEMPERATURE]}")

    for state in climate_history:
        state_time_utc = state.last_updated
        if not (dt_util.as_utc(setback_search_start_local) <= state_time_utc < dt_util.as_utc(setback_search_end_local)):
            if state_time_utc < dt_util.as_utc(setback_search_start_local) and state_time_utc > initial_climate_state_utc and state.attributes.get(ATTR_TEMPERATURE):
                 try: previous_setpoint_for_drop_check = float(state.attributes[ATTR_TEMPERATURE])
                 except (ValueError, TypeError): pass
            continue

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
        _LOGGER.warning("Could not dynamically detect overnight setback start time.")
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
        if state_time_utc < actual_overnight_start_dt_utc: continue

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
        _LOGGER.warning("Could not dynamically detect morning recovery start time.")
        return metrics_results
    metrics_results[METRIC_ACTUAL_RECOVERY_START_TIME] = actual_recovery_start_dt_utc.isoformat()
    metrics_results[METRIC_DAYTIME_TARGET_DETECTED] = daytime_target_setpoint
    actual_overnight_end_dt_utc = actual_recovery_start_dt_utc

    # --- 3. Detect Actual Morning Recovery End Time ---
    actual_recovery_end_dt_utc: Optional[datetime] = None
    max_recovery_search_end_utc = dt_util.as_utc(analysis_day_local.replace(
        hour=parse_time_string(MAX_RECOVERY_SEARCH_END_TIME).hour,
        minute=parse_time_string(MAX_RECOVERY_SEARCH_END_TIME).minute
    ))
    
    recovery_phase_query_start_utc = actual_recovery_start_dt_utc - timedelta(minutes=1)
    indoor_temp_recovery_history = await _get_historical_states(hass, recovery_phase_query_start_utc, max_recovery_search_end_utc, indoor_temp_id)
    climate_recovery_phase_history = [s for s in climate_history if actual_recovery_start_dt_utc <= s.last_updated <= max_recovery_search_end_utc]

    last_hvac_action_change_time_utc: Optional[datetime] = actual_recovery_start_dt_utc # Initialize with recovery start
    last_hvac_action: Optional[str] = None
    for state in climate_recovery_phase_history: # Get initial HVAC action at recovery start
        if state.last_updated >= actual_recovery_start_dt_utc:
            last_hvac_action = state.attributes.get(ATTR_HVAC_ACTION)
            last_hvac_action_change_time_utc = state.last_updated
            break
    
    if daytime_target_setpoint is not None:
        for temp_state in indoor_temp_recovery_history:
            temp_state_time_utc = temp_state.last_updated
            if temp_state_time_utc < actual_recovery_start_dt_utc: continue
            try: current_indoor_temp = float(temp_state.state)
            except (ValueError, TypeError): continue

            for climate_state in climate_recovery_phase_history:
                if climate_state.last_updated <= temp_state_time_utc and climate_state.last_updated >= last_hvac_action_change_time_utc :
                    new_action = climate_state.attributes.get(ATTR_HVAC_ACTION)
                    if new_action != last_hvac_action:
                        last_hvac_action = new_action
                        last_hvac_action_change_time_utc = climate_state.last_updated
            
            if current_indoor_temp >= (daytime_target_setpoint - RECOVERY_TEMP_TOLERANCE_C):
                if last_hvac_action != HVAC_ACTION_HEATING and last_hvac_action_change_time_utc is not None:
                    idle_duration_seconds = (temp_state_time_utc - last_hvac_action_change_time_utc).total_seconds()
                    if idle_duration_seconds >= MIN_IDLE_DURATION_FOR_RECOVERY_END_S:
                        actual_recovery_end_dt_utc = temp_state_time_utc 
                        _LOGGER.info(f"Dynamic Recovery End DETECTED: {actual_recovery_end_dt_utc} (Local: {dt_util.as_local(actual_recovery_end_dt_utc)})")
                        break
            if temp_state_time_utc >= max_recovery_search_end_utc: break
    
    if not actual_recovery_end_dt_utc:
        _LOGGER.warning(f"Could not dynamically detect recovery end time by {max_recovery_search_end_utc}. Using max search time as fallback if system was still heating.")
        final_climate_state_at_max_recovery = None
        for state in reversed(climate_recovery_phase_history): # Check state at the very end of search
            if state.last_updated <= max_recovery_search_end_utc:
                final_climate_state_at_max_recovery = state
                break
        if final_climate_state_at_max_recovery and final_climate_state_at_max_recovery.attributes.get(ATTR_HVAC_ACTION) == HVAC_ACTION_HEATING:
             actual_recovery_end_dt_utc = max_recovery_search_end_utc
    metrics_results[METRIC_ACTUAL_RECOVERY_END_TIME] = actual_recovery_end_dt_utc.isoformat() if actual_recovery_end_dt_utc else None
    
    # --- 4. Calculate Metrics using Dynamic Times ---
    if actual_overnight_start_dt_utc and actual_overnight_end_dt_utc:
        outdoor_temp_states_overnight = await _get_historical_states(hass, actual_overnight_start_dt_utc, actual_overnight_end_dt_utc, outdoor_temp_id)
        valid_ot = [float(s.state) for s in outdoor_temp_states_overnight if s.state is not None and s.state not in ("unknown", "unavailable") and isinstance(s.state, (str, int, float)) and str(s.state).replace('.', '', 1).replace('-', '', 1).isdigit()]
        metrics_results[METRIC_AVG_OUTDOOR_TEMP_OVERNIGHT] = round(sum(valid_ot) / len(valid_ot), 1) if valid_ot else None

        indoor_temp_states_overnight = await _get_historical_states(hass, actual_overnight_start_dt_utc, actual_overnight_end_dt_utc, indoor_temp_id)
        valid_it = [float(s.state) for s in indoor_temp_states_overnight if s.state is not None and s.state not in ("unknown", "unavailable") and isinstance(s.state, (str, int, float)) and str(s.state).replace('.', '', 1).replace('-', '', 1).isdigit()]
        metrics_results[METRIC_MIN_INDOOR_TEMP_SETBACK] = round(min(valid_it), 2) if valid_it else None
    
    # --- Gas Consumption Calculation ---
    metrics_results[METRIC_GAS_OVERNIGHT] = await _calculate_gas_for_period_utc(
        hass, gas_id, actual_overnight_start_dt_utc, actual_overnight_end_dt_utc
    )
    metrics_results[METRIC_GAS_RECOVERY] = await _calculate_gas_for_period_utc(
        hass, gas_id, actual_recovery_start_dt_utc, actual_recovery_end_dt_utc
    )

    # --- 5. Logic for "Optimum Setpoint" (Placeholder) ---
    metrics_results[METRIC_OPTIMUM_SETPOINT] = 16.0 

    _LOGGER.debug(f"Final calculated metrics: {metrics_results}")
    return metrics_results

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
