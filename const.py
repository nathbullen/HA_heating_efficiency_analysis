
DOMAIN = "heating_analyser"

# Configuration Keys (used in config_flow.py and __init__.py)
CONF_GAS_SENSOR = "gas_sensor_entity_id"
CONF_INDOOR_TEMP_SENSOR = "indoor_temp_entity_id"
CONF_OUTDOOR_TEMP_SENSOR = "outdoor_temp_entity_id"
CONF_CLIMATE_ENTITY = "climate_entity_id"

# Default sensor names / unique_id prefixes
DEFAULT_NAME = "Heating Analyser"

# Analysis period definitions (example, can be made configurable later)
OVERNIGHT_START_TIME = "22:00:00" # Local time
OVERNIGHT_END_TIME = "06:00:00"   # Local time
RECOVERY_END_TIME = "09:00:00"     # Local time

# Update interval for sensors (e.g., daily)
UPDATE_HOUR = 11 # 11 AM local time
UPDATE_MINUTE = 0
