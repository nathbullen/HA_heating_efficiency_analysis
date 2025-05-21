# custom_components/heating_analyzer/const.py

DOMAIN = "heating_analyzer"

# Configuration Keys (from config_flow.py)
CONF_GAS_SENSOR = "gas_sensor_entity_id"
CONF_INDOOR_TEMP_SENSOR = "indoor_temp_entity_id"
CONF_OUTDOOR_TEMP_SENSOR = "outdoor_temp_entity_id"
CONF_CLIMATE_ENTITY = "climate_entity_id"

DEFAULT_NAME = "Heating Analyser"

# --- Parameters for Dynamic Time Detection ---
# Windows are in HH:MM:SS local time format
# Window to search for the start of overnight setback
SETBACK_START_SEARCH_WINDOW_BEGIN = "21:00:00" # e.g., 8 PM
SETBACK_START_SEARCH_WINDOW_END = "08:00:00"   # e.g., Midnight

# Window to search for the start of morning recovery
RECOVERY_START_SEARCH_WINDOW_BEGIN = "05:00:00" # e.g., 3 AM
RECOVERY_START_SEARCH_WINDOW_END = "10:00:00"   # e.g., 8 AM

# Maximum time to search for the end of morning recovery
MAX_RECOVERY_SEARCH_END_TIME = "10:45:00" # e.g., 10 AM, to avoid including all day heating

# Thresholds for detecting setpoint changes
SIGNIFICANT_SETPOINT_DROP_C = 1.5  # Min °C drop to identify setback start
SIGNIFICANT_SETPOINT_RISE_C = 1.5  # Min °C rise to identify recovery start
TYPICAL_SETBACK_TEMP_MIN = 12.0    # Min expected setback temperature
TYPICAL_SETBACK_TEMP_MAX = 15.0    # Max expected setback temperature (below daytime)
TYPICAL_DAYTIME_TEMP_MIN = 18.0    # Typical daytime setpoint

# Parameters for detecting recovery end
RECOVERY_TEMP_TOLERANCE_C = 0.3 # How close indoor temp needs to be to target
MIN_IDLE_DURATION_FOR_RECOVERY_END_S = 300 # Min seconds climate must be idle (5 mins)

# Daily update time for the analysis
UPDATE_HOUR = 11 # 11 AM local time
UPDATE_MINUTE = 30

# --- Constants for returned metrics keys (optional, for consistency) ---
METRIC_OPTIMUM_SETPOINT = "optimum_setpoint"
METRIC_AVG_OUTDOOR_TEMP_OVERNIGHT = "avg_outdoor_temp_overnight"
METRIC_MIN_INDOOR_TEMP_SETBACK = "min_indoor_temp_setback"
METRIC_GAS_OVERNIGHT = "gas_used_overnight"
METRIC_GAS_RECOVERY = "gas_used_recovery"
METRIC_ACTUAL_OVERNIGHT_START_TIME = "actual_overnight_start_time"
METRIC_ACTUAL_RECOVERY_START_TIME = "actual_recovery_start_time"
METRIC_ACTUAL_RECOVERY_END_TIME = "actual_recovery_end_time"
METRIC_OVERNIGHT_SETPOINT_DETECTED = "overnight_setpoint_detected"
METRIC_DAYTIME_TARGET_DETECTED = "daytime_target_detected"
