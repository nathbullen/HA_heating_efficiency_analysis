# custom_components/heating_analyzer/__init__.py

import logging
from collections import defaultdict
from datetime import time, timedelta, datetime
from typing import Optional, Dict, Any, List, Tuple

from homeassistant.core import HomeAssistant, State
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util
from homeassistant.components.recorder import get_instance as r_get_instance
from homeassistant.components.recorder.history import (
    get_significant_states,
    get_state,
    state_changes_during_period,
)
# Import for Long-Term Statistics
from homeassistant.components.recorder.statistics import (
    statistics_during_period,
    get_last_statistics, # Potentially useful for getting the very last LTS point
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
    OPTIMUM_SETPOINT_HISTORY_DAYS,
    OUTDOOR_TEMP_CATEGORY_VERY_COLD_MAX_C,
    OUTDOOR_TEMP_CATEGORY_COLD_MAX_C,
    MIN_DATA_POINTS_FOR_OPTIMUM_REC,
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
            current_day_metrics = await async_calculate_daily_operational_metrics(
                hass,
                config_data[CONF_INDOOR_TEMP_SENSOR],
                config_data[CONF_OUTDOOR_TEMP_SENSOR],
                config_data[CONF_CLIMATE_ENTITY],
                config_data[CONF_GAS_SENSOR]
            )
            
            if not isinstance(current_day_metrics, dict):
                _LOGGER.error(f"Heating Analyzer ({entry.title}): Daily operational metrics calculation did not return a dictionary.")
                current_day_metrics = {}

            current_avg_outdoor_temp = current_day_metrics.get(METRIC_AVG_OUTDOOR_TEMP_OVERNIGHT)
            # Use entry.entry_id for LTS function to correctly identify sensor instances
            optimum_setpoint = await async_determine_optimum_setpoint_from_lts(
                hass, 
                entry.entry_id, 
                current_avg_outdoor_temp
            )
            current_day_metrics[METRIC_OPTIMUM_SETPOINT] = optimum_setpoint
            
            sensors_dict = hass.data[DOMAIN][entry.entry_id].get('sensors', {})
            if not sensors_dict:
                _LOGGER.warning(f"Heating Analyzer ({entry.title}): Sensor entities not found for update.")
            else:
                for metric_key, sensor_instance in sensors_dict.items():
                    value_to_update = current_day_metrics.get(metric_key)
                    if metric_key in current_day_metrics:
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


async def _get_historical_states(hass: HomeAssistant, start_utc: datetime, end_utc: datetime, entity_id: str, significant_changes_only=False):
    """Helper to fetch states for an entity within a period from detailed history."""
    # This function is still used for the daily operational metrics from source sensors
    _LOGGER.debug(f"Fetching DETAILED history for {entity_id} from {start_utc} to {end_utc} (significant_only={significant_changes_only})")
    if not isinstance(start_utc, datetime) or not isinstance(end_utc, datetime):
        _LOGGER.error(f"Invalid datetime objects for history query: start={start_utc}, end={end_utc}")
        return []
    history = await r_get_instance(hass).async_add_executor_job(
        get_significant_states, hass, start_utc, end_utc, [entity_id],
        include_start_time_state=True, significant_changes_only=significant_changes_only
    )
    return history.get(entity_id, [])

def parse_time_string(time_str: str) -> time:
    return datetime.strptime(time_str, "%H:%M:%S").time()

async def _calculate_gas_for_period_utc(
    hass: HomeAssistant, 
    gas_entity_id: str, 
    period_start_utc: Optional[datetime], 
    period_end_utc: Optional[datetime]
) -> Optional[float]:
    # (Gas calculation logic remains the same as v0.4.0 - it uses detailed history for accuracy)
    if not gas_entity_id or not period_start_utc or not period_end_utc: return None
    if period_start_utc >= period_end_utc: return 0.0
    _LOGGER.debug(f"Calculating gas for {gas_entity_id} from {period_start_utc} to {period_end_utc}")
    try:
        history_data = await r_get_instance(hass).async_add_executor_job(
            state_changes_during_period, hass, period_start_utc, period_end_utc,
            gas_entity_id, include_start_time_state=True, no_attributes=True
        )
    except Exception as e:
        _LOGGER.error(f"Error fetching history for gas sensor {gas_entity_id}: {e}")
        return None
    states = history_data.get(gas_entity_id, [])
    if not states:
        _LOGGER.warning(f"No gas history for {gas_entity_id} in period {period_start_utc} to {period_end_utc}.")
        start_state_obj = await r_get_instance(hass).async_add_executor_job(get_state, hass, period_start_utc, gas_entity_id)
        end_state_obj = await r_get_instance(hass).async_add_executor_job(get_state, hass, period_end_utc, gas_entity_id)
        if start_state_obj and end_state_obj and start_state_obj.state is not None and end_state_obj.state is not None:
            try:
                start_val = float(start_state_obj.state)
                end_val = float(end_state_obj.state)
                if end_val >= start_val: return round(end_val - start_val, 3)
                _LOGGER.warning(f"Gas sensor {gas_entity_id} end value < start with no intermediate states. Ambiguous.")
                return None
            except (ValueError, TypeError): return None
        return None
    calculated_consumption = 0.0
    segment_start_value = 0.0 
    start_val_obj = await r_get_instance(hass).async_add_executor_job(get_state, hass, period_start_utc, gas_entity_id)
    if start_val_obj and start_val_obj.state is not None:
        try: segment_start_value = float(start_val_obj.state)
        except (ValueError, TypeError): _LOGGER.error("Bad start val"); return None
    elif states and states[0].last_updated <= period_start_utc + timedelta(minutes=5):
        try: segment_start_value = float(states[0].state)
        except (ValueError, TypeError): _LOGGER.error("Bad first state val"); return None
    else:
        _LOGGER.warning(f"No reliable start gas value for {gas_entity_id} at {period_start_utc}")
        return None
    last_val = segment_start_value
    for state_obj in states:
        if state_obj.last_updated <= period_start_utc : continue
        if state_obj.last_updated > period_end_utc: break
        try: current_val = float(state_obj.state)
        except (ValueError, TypeError): continue
        if current_val < last_val: 
            calculated_consumption += last_val 
            segment_start_value = 0 
        last_val = current_val
    calculated_consumption += (last_val - segment_start_value)
    _LOGGER.info(f"Calculated gas for {gas_entity_id} from {period_start_utc} to {period_end_utc}: {round(calculated_consumption, 3)}")
    return round(calculated_consumption, 3) if calculated_consumption >= 0 else 0.0


async def async_calculate_daily_operational_metrics(
    hass: HomeAssistant, 
    indoor_temp_id: str, 
    outdoor_temp_id: str, 
    climate_id: str, 
    gas_id: str
) -> Dict[str, Any]:
    """Calculates daily operational metrics using detailed recent history."""
    # (This function's core logic for dynamic time detection and calculating daily metrics
    # remains the same as in v0.4.0, using _get_historical_states for detailed history.
    # For brevity, the full dynamic time detection logic is not repeated here but assumed to be the same.)
    _LOGGER.debug(f"Calculating daily operational metrics using detailed history...")
    metrics_results: Dict[str, Any] = {
        METRIC_AVG_OUTDOOR_TEMP_OVERNIGHT: None, METRIC_MIN_INDOOR_TEMP_SETBACK: None,
        METRIC_GAS_OVERNIGHT: None, METRIC_GAS_RECOVERY: None,
        METRIC_ACTUAL_OVERNIGHT_START_TIME: None, METRIC_ACTUAL_RECOVERY_START_TIME: None,
        METRIC_ACTUAL_RECOVERY_END_TIME: None, METRIC_OVERNIGHT_SETPOINT_DETECTED: None,
        METRIC_DAYTIME_TARGET_DETECTED: None,
    }
    now_local = dt_util.as_local(dt_util.utcnow())
    analysis_day_local = now_local
    max_recovery_time_today_local = analysis_day_local.replace(hour=parse_time_string(MAX_RECOVERY_SEARCH_END_TIME).hour, minute=parse_time_string(MAX_RECOVERY_SEARCH_END_TIME).minute, second=0, microsecond=0)
    if now_local < max_recovery_time_today_local:
        analysis_day_local -= timedelta(days=1)
    query_period_start_local = (analysis_day_local - timedelta(days=1)).replace(hour=parse_time_string(SETBACK_START_SEARCH_WINDOW_BEGIN).hour, minute=parse_time_string(SETBACK_START_SEARCH_WINDOW_BEGIN).minute)
    query_period_end_local = analysis_day_local.replace(hour=parse_time_string(MAX_RECOVERY_SEARCH_END_TIME).hour, minute=parse_time_string(MAX_RECOVERY_SEARCH_END_TIME).minute)
    query_start_utc = dt_util.as_utc(query_period_start_local)
    query_end_utc = dt_util.as_utc(query_period_end_local)
    climate_history = await _get_historical_states(hass, query_start_utc, query_end_utc, climate_id)
    if not climate_history: return metrics_results

    # --- [Dynamic Time Detection Logic - assume it populates these variables as in v0.4.0] ---
    actual_overnight_start_dt_utc: Optional[datetime] = None; overnight_setpoint_detected: Optional[float] = None
    actual_recovery_start_dt_utc: Optional[datetime] = None; daytime_target_setpoint: Optional[float] = None
    actual_recovery_end_dt_utc: Optional[datetime] = None; actual_overnight_end_dt_utc: Optional[datetime] = None
    # --- Start of condensed detection logic (from previous version) ---
    previous_setpoint_for_drop_check: Optional[float] = None
    setback_search_start_local = (analysis_day_local - timedelta(days=1)).replace(hour=parse_time_string(SETBACK_START_SEARCH_WINDOW_BEGIN).hour, minute=parse_time_string(SETBACK_START_SEARCH_WINDOW_BEGIN).minute)
    setback_search_end_time_obj = parse_time_string(SETBACK_START_SEARCH_WINDOW_END)
    if setback_search_end_time_obj < parse_time_string(SETBACK_START_SEARCH_WINDOW_BEGIN): setback_search_end_local = analysis_day_local.replace(hour=setback_search_end_time_obj.hour, minute=setback_search_end_time_obj.minute)
    else: setback_search_end_local = (analysis_day_local - timedelta(days=1)).replace(hour=setback_search_end_time_obj.hour, minute=setback_search_end_time_obj.minute)
    initial_climate_state_utc = dt_util.as_utc(setback_search_start_local - timedelta(seconds=1))
    initial_state_obj = await r_get_instance(hass).async_add_executor_job(get_state, hass, initial_climate_state_utc, climate_id)
    if initial_state_obj and initial_state_obj.attributes.get(ATTR_TEMPERATURE):
        try: previous_setpoint_for_drop_check = float(initial_state_obj.attributes[ATTR_TEMPERATURE])
        except: pass
    for state in climate_history:
        state_time_utc = state.last_updated
        if not (dt_util.as_utc(setback_search_start_local) <= state_time_utc < dt_util.as_utc(setback_search_end_local)):
            if state_time_utc < dt_util.as_utc(setback_search_start_local) and state_time_utc > initial_climate_state_utc and state.attributes.get(ATTR_TEMPERATURE):
                 try: previous_setpoint_for_drop_check = float(state.attributes[ATTR_TEMPERATURE])
                 except: pass
            continue
        current_setpoint_attr = state.attributes.get(ATTR_TEMPERATURE)
        if current_setpoint_attr is None: continue
        try: current_setpoint = float(current_setpoint_attr)
        except: continue
        if previous_setpoint_for_drop_check is not None and (previous_setpoint_for_drop_check - current_setpoint >= SIGNIFICANT_SETPOINT_DROP_C and TYPICAL_SETBACK_TEMP_MIN <= current_setpoint <= TYPICAL_SETBACK_TEMP_MAX):
            actual_overnight_start_dt_utc = state_time_utc; overnight_setpoint_detected = current_setpoint; break 
        previous_setpoint_for_drop_check = current_setpoint
    if not actual_overnight_start_dt_utc: return metrics_results
    metrics_results[METRIC_ACTUAL_OVERNIGHT_START_TIME] = actual_overnight_start_dt_utc.isoformat(); metrics_results[METRIC_OVERNIGHT_SETPOINT_DETECTED] = overnight_setpoint_detected
    previous_setpoint_for_rise_check = overnight_setpoint_detected 
    recovery_search_start_local = analysis_day_local.replace(hour=parse_time_string(RECOVERY_START_SEARCH_WINDOW_BEGIN).hour, minute=parse_time_string(RECOVERY_START_SEARCH_WINDOW_BEGIN).minute)
    recovery_search_end_local = analysis_day_local.replace(hour=parse_time_string(RECOVERY_START_SEARCH_WINDOW_END).hour, minute=parse_time_string(RECOVERY_START_SEARCH_WINDOW_END).minute)
    for state in climate_history:
        state_time_utc = state.last_updated
        if state_time_utc < actual_overnight_start_dt_utc: continue
        if not (dt_util.as_utc(recovery_search_start_local) <= state_time_utc < dt_util.as_utc(recovery_search_end_local)):
            if state_time_utc < dt_util.as_utc(recovery_search_start_local) and state.attributes.get(ATTR_TEMPERATURE):
                try: previous_setpoint_for_rise_check = float(state.attributes[ATTR_TEMPERATURE])
                except: pass
            continue
        current_setpoint_attr = state.attributes.get(ATTR_TEMPERATURE)
        if current_setpoint_attr is None: continue
        try: current_setpoint = float(current_setpoint_attr)
        except: continue
        if previous_setpoint_for_rise_check is not None and (current_setpoint - previous_setpoint_for_rise_check >= SIGNIFICANT_SETPOINT_RISE_C and current_setpoint >= TYPICAL_DAYTIME_TEMP_MIN):
            actual_recovery_start_dt_utc = state_time_utc; daytime_target_setpoint = current_setpoint; break
        previous_setpoint_for_rise_check = current_setpoint
    if not actual_recovery_start_dt_utc: return metrics_results
    metrics_results[METRIC_ACTUAL_RECOVERY_START_TIME] = actual_recovery_start_dt_utc.isoformat(); metrics_results[METRIC_DAYTIME_TARGET_DETECTED] = daytime_target_setpoint
    actual_overnight_end_dt_utc = actual_recovery_start_dt_utc
    max_recovery_search_end_utc = dt_util.as_utc(analysis_day_local.replace(hour=parse_time_string(MAX_RECOVERY_SEARCH_END_TIME).hour, minute=parse_time_string(MAX_RECOVERY_SEARCH_END_TIME).minute))
    recovery_phase_query_start_utc = actual_recovery_start_dt_utc - timedelta(minutes=1)
    indoor_temp_recovery_history = await _get_historical_states(hass, recovery_phase_query_start_utc, max_recovery_search_end_utc, indoor_temp_id)
    climate_recovery_phase_history = [s for s in climate_history if actual_recovery_start_dt_utc <= s.last_updated <= max_recovery_search_end_utc]
    last_hvac_action_change_time_utc: Optional[datetime] = actual_recovery_start_dt_utc; last_hvac_action: Optional[str] = None
    for state in climate_recovery_phase_history:
        if state.last_updated >= actual_recovery_start_dt_utc: last_hvac_action = state.attributes.get(ATTR_HVAC_ACTION); last_hvac_action_change_time_utc = state.last_updated; break
    if daytime_target_setpoint is not None:
        for temp_state in indoor_temp_recovery_history:
            temp_state_time_utc = temp_state.last_updated
            if temp_state_time_utc < actual_recovery_start_dt_utc: continue
            try: current_indoor_temp = float(temp_state.state)
            except: continue
            for climate_state in climate_recovery_phase_history:
                if climate_state.last_updated <= temp_state_time_utc and climate_state.last_updated >= last_hvac_action_change_time_utc :
                    new_action = climate_state.attributes.get(ATTR_HVAC_ACTION)
                    if new_action != last_hvac_action: last_hvac_action = new_action; last_hvac_action_change_time_utc = climate_state.last_updated
            if current_indoor_temp >= (daytime_target_setpoint - RECOVERY_TEMP_TOLERANCE_C) and last_hvac_action != HVAC_ACTION_HEATING and last_hvac_action_change_time_utc is not None and (temp_state_time_utc - last_hvac_action_change_time_utc).total_seconds() >= MIN_IDLE_DURATION_FOR_RECOVERY_END_S:
                actual_recovery_end_dt_utc = temp_state_time_utc ; break
            if temp_state_time_utc >= max_recovery_search_end_utc: break
    if not actual_recovery_end_dt_utc:
        final_climate_state_at_max_recovery = next((s for s in reversed(climate_recovery_phase_history) if s.last_updated <= max_recovery_search_end_utc), None)
        if final_climate_state_at_max_recovery and final_climate_state_at_max_recovery.attributes.get(ATTR_HVAC_ACTION) == HVAC_ACTION_HEATING: actual_recovery_end_dt_utc = max_recovery_search_end_utc
    metrics_results[METRIC_ACTUAL_RECOVERY_END_TIME] = actual_recovery_end_dt_utc.isoformat() if actual_recovery_end_dt_utc else None
    # --- End of condensed detection logic ---

    if actual_overnight_start_dt_utc and actual_overnight_end_dt_utc:
        outdoor_temp_states = await _get_historical_states(hass, actual_overnight_start_dt_utc, actual_overnight_end_dt_utc, outdoor_temp_id)
        valid_ot = [float(s.state) for s in outdoor_temp_states if s.state is not None and s.state not in ("unknown", "unavailable") and str(s.state).replace('.', '', 1).replace('-', '', 1).isdigit()]
        metrics_results[METRIC_AVG_OUTDOOR_TEMP_OVERNIGHT] = round(sum(valid_ot) / len(valid_ot), 1) if valid_ot else None
        indoor_temp_states = await _get_historical_states(hass, actual_overnight_start_dt_utc, actual_overnight_end_dt_utc, indoor_temp_id)
        valid_it = [float(s.state) for s in indoor_temp_states if s.state is not None and s.state not in ("unknown", "unavailable") and str(s.state).replace('.', '', 1).replace('-', '', 1).isdigit()]
        metrics_results[METRIC_MIN_INDOOR_TEMP_SETBACK] = round(min(valid_it), 2) if valid_it else None
    
    metrics_results[METRIC_GAS_OVERNIGHT] = await _calculate_gas_for_period_utc(hass, gas_id, actual_overnight_start_dt_utc, actual_overnight_end_dt_utc)
    metrics_results[METRIC_GAS_RECOVERY] = await _calculate_gas_for_period_utc(hass, gas_id, actual_recovery_start_dt_utc, actual_recovery_end_dt_utc)
    
    _LOGGER.debug(f"Daily operational metrics calculated: {metrics_results}")
    return metrics_results


def _categorize_outdoor_temp(avg_temp: Optional[float]) -> str:
    if avg_temp is None: return "Unknown"
    if avg_temp <= OUTDOOR_TEMP_CATEGORY_VERY_COLD_MAX_C: return "Very Cold"
    if avg_temp <= OUTDOOR_TEMP_CATEGORY_COLD_MAX_C: return "Cold"
    return "Mild"

async def async_determine_optimum_setpoint_from_lts(
    hass: HomeAssistant,
    config_entry_id: str, 
    current_day_avg_outdoor_temp: Optional[float]
) -> Optional[float]:
    """
    Analyzes historical Long-Term Statistics (LTS) of daily metrics 
    to recommend an optimum setpoint.
    """
    _LOGGER.info(f"Determining optimum setpoint for entry {config_entry_id} using LTS...")
    if current_day_avg_outdoor_temp is None:
        _LOGGER.warning("Cannot determine optimum setpoint from LTS: current day's average outdoor temperature is unknown.")
        return None

    current_temp_category = _categorize_outdoor_temp(current_day_avg_outdoor_temp)
    _LOGGER.debug(f"Current day's outdoor temp: {current_day_avg_outdoor_temp}°C, Category: {current_temp_category}")

    history_end_utc = dt_util.utcnow() # Analyze up to now
    history_start_utc = history_end_utc - timedelta(days=OPTIMUM_SETPOINT_HISTORY_DAYS)

    # --- Get Entity IDs of the daily metric sensors stored by sensor.py ---
    entry_data_store = hass.data.get(DOMAIN, {}).get(config_entry_id, {})
    sensors_in_entry = entry_data_store.get('sensors', {})
    
    # Prepare a list of statistic_ids (which are the entity_ids of our daily sensors)
    # Ensure these metric keys match what's stored in sensors_in_entry by sensor.py
    metric_keys_for_lts = [
        METRIC_AVG_OUTDOOR_TEMP_OVERNIGHT,
        METRIC_OVERNIGHT_SETPOINT_DETECTED,
        METRIC_GAS_OVERNIGHT,
        METRIC_GAS_RECOVERY,
    ]
    statistic_ids_to_query: List[str] = []
    for key in metric_keys_for_lts:
        sensor_instance = sensors_in_entry.get(key)
        if sensor_instance and hasattr(sensor_instance, 'entity_id') and sensor_instance.entity_id:
            statistic_ids_to_query.append(sensor_instance.entity_id)
        else:
            _LOGGER.error(f"Could not find valid entity_id for metric key '{key}' to query LTS. Optimum setpoint calculation may be incomplete.")
            # Potentially return None here if critical IDs are missing
            # For now, we'll proceed if some are found, but log the issue.

    if len(statistic_ids_to_query) < len(metric_keys_for_lts): # Check if all critical sensors were found
         _LOGGER.warning(f"Missing some entity_ids for LTS query. Found: {statistic_ids_to_query}. Required for keys: {metric_keys_for_lts}")
         # Decide if to proceed or return None
         # return None # Stricter: if not all data sources available, don't recommend

    if not statistic_ids_to_query:
        _LOGGER.error("No valid statistic_ids found for LTS query. Aborting optimum setpoint calculation.")
        return None

    _LOGGER.debug(f"Querying LTS for statistic_ids: {statistic_ids_to_query} from {history_start_utc} to {history_end_utc}")

    try:
        # For daily sensors, 'mean' or 'state' should capture the day's value.
        # 'state' might be preferred if the sensor only records one state per day.
        # Let's try 'mean' as it's commonly available for numeric measurement sensors in LTS.
        # If 'state' is available and more direct, that could be used.
        # The type 'sum' might be relevant if gas sensors were total_increasing and we wanted daily sum from LTS.
        # But our gas sensors are daily measurements, so 'mean' (which will be the value) is fine.
        lts_data = await hass.async_add_executor_job(
            statistics_during_period,
            hass,
            history_start_utc,
            history_end_utc,
            statistic_ids_to_query,
            period="day", # Aggregate by day
            types={"mean"}, # For sensors that update once a day, mean will be the value.
        )
    except Exception as e:
        _LOGGER.error(f"Error fetching LTS data: {e}", exc_info=True)
        return None

    # --- Process and Align Historical LTS Data by Day ---
    daily_historical_data: Dict[datetime.date, Dict[str, Any]] = defaultdict(dict)

    # Map entity_id back to our metric_key for easier processing
    entity_id_to_metric_key_map: Dict[str, str] = {}
    for key in metric_keys_for_lts:
        sensor_instance = sensors_in_entry.get(key)
        if sensor_instance and hasattr(sensor_instance, 'entity_id') and sensor_instance.entity_id:
            entity_id_to_metric_key_map[sensor_instance.entity_id] = key
            
    for entity_id, daily_stats in lts_data.items():
        metric_key_for_dict = entity_id_to_metric_key_map.get(entity_id)
        if not metric_key_for_dict:
            _LOGGER.warning(f"Could not map LTS entity_id {entity_id} back to a metric key. Skipping.")
            continue

        for daily_stat_point in daily_stats:
            # 'start' is a timestamp string in UTC from LTS
            start_dt_utc = dt_util.parse_datetime(daily_stat_point["start"])
            if not start_dt_utc: continue # Should not happen

            event_date = dt_util.as_local(start_dt_utc).date()
            value = daily_stat_point.get("mean") # Or "state" if that's what LTS provides for these

            if value is not None:
                try:
                    # Map our internal metric keys to simpler keys for the daily_historical_data dict
                    if metric_key_for_dict == METRIC_AVG_OUTDOOR_TEMP_OVERNIGHT:
                        daily_historical_data[event_date]["outdoor_temp"] = float(value)
                    elif metric_key_for_dict == METRIC_OVERNIGHT_SETPOINT_DETECTED:
                        daily_historical_data[event_date]["setpoint"] = float(value)
                    elif metric_key_for_dict == METRIC_GAS_OVERNIGHT:
                        daily_historical_data[event_date]["gas_on"] = float(value)
                    elif metric_key_for_dict == METRIC_GAS_RECOVERY:
                        daily_historical_data[event_date]["gas_rec"] = float(value)
                except (ValueError, TypeError):
                    _LOGGER.debug(f"Could not parse LTS value {value} for {metric_key_for_dict} on {event_date}")
            else:
                _LOGGER.debug(f"LTS data point for {entity_id} on {event_date} had None for 'mean'. Stat point: {daily_stat_point}")


    # --- Aggregate performance by setpoint and outdoor temp category ---
    performance_data: Dict[str, Dict[float, List[float]]] = defaultdict(lambda: defaultdict(list))
    processed_days = 0
    for day_date, metrics in daily_historical_data.items():
        # Ensure all required metrics for a day are present after LTS processing
        required_keys = ["outdoor_temp", "setpoint", "gas_on", "gas_rec"]
        if all(k in metrics for k in required_keys):
            outdoor_temp = metrics["outdoor_temp"]
            setpoint = metrics["setpoint"]
            gas_on = metrics["gas_on"] # Already float or None
            gas_rec = metrics["gas_rec"] # Already float or None

            if gas_on is None or gas_rec is None: # Skip day if gas data is missing
                _LOGGER.debug(f"Skipping day {day_date} due to missing gas data in LTS (gas_on: {gas_on}, gas_rec: {gas_rec})")
                continue

            total_gas = gas_on + gas_rec
            temp_category = _categorize_outdoor_temp(outdoor_temp)
            performance_data[temp_category][setpoint].append(total_gas)
            processed_days += 1
            _LOGGER.debug(f"LTS Historical point: Date {day_date}, TempCat {temp_category}, Setpoint {setpoint}, TotalGas {total_gas:.2f}")
        else:
            _LOGGER.debug(f"Skipping day {day_date} from LTS due to incomplete metrics: {metrics}. Required: {required_keys}")

    _LOGGER.info(f"Processed {processed_days} complete daily records from LTS for optimum setpoint analysis.")
    if processed_days == 0:
        _LOGGER.warning("No complete daily records found in LTS for the specified period. Cannot determine optimum setpoint.")
        return None

    # --- Find optimum setpoint for the current day's temperature category ---
    if current_temp_category not in performance_data or not performance_data[current_temp_category]:
        _LOGGER.info(f"No historical LTS data found for current temperature category: {current_temp_category}.")
        return None

    best_setpoint: Optional[float] = None
    min_avg_gas: float = float('inf')
    category_performance = performance_data[current_temp_category]
    _LOGGER.debug(f"LTS Performance data for category '{current_temp_category}': {category_performance}")

    for setpoint, gas_values in category_performance.items():
        if len(gas_values) >= MIN_DATA_POINTS_FOR_OPTIMUM_REC:
            avg_gas = sum(gas_values) / len(gas_values)
            _LOGGER.debug(f"  LTS Analysis - Setpoint {setpoint}: Avg Gas = {avg_gas:.2f} from {len(gas_values)} points.")
            if avg_gas < min_avg_gas:
                min_avg_gas = avg_gas
                best_setpoint = setpoint
        else:
            _LOGGER.debug(f"  LTS Analysis - Setpoint {setpoint}: Insufficient data ({len(gas_values)} points), needs {MIN_DATA_POINTS_FOR_OPTIMUM_REC}.")

    if best_setpoint is not None:
        _LOGGER.info(f"Optimum setpoint from LTS for category '{current_temp_category}' determined to be {best_setpoint}°C with avg gas {min_avg_gas:.2f} MJ/units.")
    else:
        _LOGGER.info(f"Could not determine an optimum setpoint from LTS for category '{current_temp_category}' due to insufficient historical data points meeting criteria.")
    return best_setpoint


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
