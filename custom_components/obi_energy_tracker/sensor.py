"""Sensor platform for Obi EnergyTracker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ObiEnergyTrackerConfigEntry
from .const import DOMAIN
from .coordinator import ObiEnergyTrackerCoordinator
from .live import ObiLiveMode

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ObiEnergyTrackerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    coordinator = config_entry.runtime_data

    sensors: list[SensorEntity] = [
        ObiLivePowerSensor(config_entry.runtime_data.live),
        ObiMeterReadingSensor(coordinator),
        ObiFeedInMeterReadingSensor(coordinator),
        ObiCurrentPowerSensor(coordinator),
        ObiCurrentFeedInPowerSensor(coordinator),
        ObiBatteryLevelSensor(coordinator),
        ObiIsOnlineSensor(coordinator),
        ObiConnectionStrengthSensor(coordinator),
        ObiLastRecordReceivedAtSensor(coordinator),
    ]

    async_add_entities(sensors)


def _extract_meter_reading(meter_data: Any, measure: str) -> float | None:
    """Extract the latest reading for a given measure from meter data.

    The meter endpoint can return either a single record (dict) or a list of
    records, each optionally tagged with a "measure" (e.g. "energy" or
    "negative_energy"). Falls back to legacy shapes ("energy"/"value" keys
    without a "measure" tag) for the "energy" measure only.
    """
    if not meter_data:
        return None

    records = meter_data if isinstance(meter_data, list) else [meter_data]
    records = [record for record in records if isinstance(record, dict)]
    if not records:
        return None

    matching = [record for record in records if record.get("measure") == measure]
    if matching:
        return matching[-1].get("value")

    if measure == "energy":
        legacy = records[-1]
        if "energy" in legacy:
            return legacy["energy"]
        if "value" in legacy:
            return legacy["value"]

    return None


class ObiEnergySensorBase(CoordinatorEntity[ObiEnergyTrackerCoordinator], SensorEntity):
    """Base class for Obi EnergyTracker sensors."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ObiEnergyTrackerCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, "obi_energy_tracker")},
            "name": "Obi EnergyTracker",
            "manufacturer": "Obi",
        }


class ObiMeterSensorBase(ObiEnergySensorBase):
    """Base sensor for cumulative meter readings (Zählerstand)."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "Wh"
    _measure: str

    def __init__(self, coordinator: ObiEnergyTrackerCoordinator) -> None:
        """Initialize the meter sensor."""
        super().__init__(coordinator)
        self._last_native_value: float | None = None
        self._last_native_value_set = False

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates and suppress duplicate readings."""
        new_value = self.native_value

        if not self._last_native_value_set or new_value != self._last_native_value:
            self._last_native_value_set = True
            self._last_native_value = new_value
            self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Return the meter reading value for this sensor's measure."""
        _LOGGER.debug(
            "%s native_value called. Data: %s",
            type(self).__name__,
            self.coordinator.data,
        )
        if not self.coordinator.data:
            return None

        return _extract_meter_reading(self.coordinator.data.get("meter"), self._measure)


class ObiMeterReadingSensor(ObiMeterSensorBase):
    """Sensor for total meter reading (Zählerstand / Bezug)."""

    _attr_unique_id = "obi_meter_reading"
    _attr_translation_key = "meter_reading"
    _measure = "energy"


class ObiFeedInMeterReadingSensor(ObiMeterSensorBase):
    """Sensor for total feed-in meter reading (Zählerstand Netzeinspeisung)."""

    _attr_unique_id = "obi_feed_in_meter_reading"
    _attr_translation_key = "feed_in_meter_reading"
    _measure = "negative_energy"


class ObiDeviceValueSensorBase(ObiEnergySensorBase):
    """Base sensor for values sourced from coordinator device data."""

    _device_key: str

    @property
    def native_value(self) -> Any:
        """Return value for the configured device key."""
        if not self.coordinator.data:
            return None

        device_data = self.coordinator.data.get("device")
        if not isinstance(device_data, dict):
            return None

        return device_data.get(self._device_key)


class ObiBatteryLevelSensor(ObiDeviceValueSensorBase):
    """Sensor for battery level."""

    _attr_unique_id = "obi_battery_level"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "battery_level"
    _attr_native_unit_of_measurement = "%"
    _device_key = "batteryLevel"


class ObiIsOnlineSensor(ObiDeviceValueSensorBase):
    """Sensor for current online state."""

    _attr_unique_id = "obi_is_online"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "is_online"
    _attr_options = ["online", "offline"]
    _device_key = "isOnline"

    @property
    def native_value(self) -> str | None:
        """Return the online status as enum value."""
        value = super().native_value
        if value is None:
            return None
        return "online" if bool(value) else "offline"


class ObiConnectionStrengthSensor(ObiDeviceValueSensorBase):
    """Sensor for connection strength reported by API."""

    _attr_unique_id = "obi_connection_strength"
    _attr_translation_key = "connection_strength"
    _device_key = "connectionStrength"


class ObiLastRecordReceivedAtSensor(ObiDeviceValueSensorBase):
    """Sensor for timestamp of the last received record."""

    _attr_unique_id = "obi_last_record_received_at"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_record_received_at"
    _device_key = "lastRecordReceivedAt"

    @property
    def native_value(self) -> datetime | None:
        """Return parsed timestamp value."""
        value = super().native_value
        if not isinstance(value, str):
            return None

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None


# Candidate keys for the timestamp of a meter record. The backend is
# undocumented and has used more than one spelling, so probe a few.
_TIMESTAMP_KEYS = (
    "timestamp",
    "time",
    "dateTime",
    "datetime",
    "date",
    "measuredAt",
    "recordedAt",
    "receivedAt",
    "createdAt",
    "at",
    "start",
    "from",
)

# A derived power value is only meaningful if the two readings are reasonably
# close together. Beyond this the device was offline and the "average" would
# smear a long outage into a wrong momentary value.
MAX_POWER_INTERVAL = timedelta(hours=1)

# Below this the quantisation of the meter reading dominates the result.
MIN_POWER_INTERVAL = timedelta(seconds=20)


def _parse_record_time(record: dict[str, Any]) -> datetime | None:
    """Return the timestamp of a meter record, if one can be found."""
    for key in _TIMESTAMP_KEYS:
        value = record.get(key)
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None


def _extract_meter_series(
    meter_data: Any, measure: str
) -> list[tuple[datetime, float]]:
    """Return the timestamped readings for a measure, oldest first.

    Records without a parsable timestamp or a numeric value are dropped, so an
    unexpected payload shape yields an empty series rather than a wrong value.
    """
    if not meter_data:
        return []

    records = meter_data if isinstance(meter_data, list) else [meter_data]
    series: list[tuple[datetime, float]] = []

    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("measure") != measure:
            # Legacy untagged shape carries the "energy" measure only.
            if measure != "energy" or "measure" in record:
                continue

        value = record.get("value", record.get("energy"))
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue

        timestamp = _parse_record_time(record)
        if timestamp is None:
            continue

        series.append((timestamp, float(value)))

    series.sort(key=lambda item: item[0])
    return series


class ObiPowerSensorBase(ObiEnergySensorBase):
    """Base sensor deriving momentary power from consecutive meter readings.

    The backend exposes no power measure, so power is the slope of the meter
    reading: the energy between the two most recent readings divided by the
    time between them. The result is therefore an average over the device's
    reporting interval, not an instantaneous value.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "W"
    _attr_suggested_display_precision = 0
    _measure: str

    def _latest_interval(self) -> tuple[datetime, float, timedelta] | None:
        """Return (end time, watts, interval) for the most recent pair."""
        if not self.coordinator.data:
            return None

        series = _extract_meter_series(
            self.coordinator.data.get("meter"), self._measure
        )
        if len(series) < 2:
            return None

        (start_time, start_value), (end_time, end_value) = series[-2], series[-1]
        interval = end_time - start_time

        if not MIN_POWER_INTERVAL <= interval <= MAX_POWER_INTERVAL:
            _LOGGER.debug(
                "%s: interval %s outside usable range, no power value",
                type(self).__name__,
                interval,
            )
            return None

        delta = end_value - start_value
        if delta < 0:
            # Meter readings only increase; a drop means a reset or a reordered
            # payload, neither of which yields a usable power value.
            return None

        watts = delta / (interval.total_seconds() / 3600)
        return end_time, watts, interval

    @property
    def native_value(self) -> float | None:
        """Return the derived power in watts."""
        result = self._latest_interval()
        if result is None:
            return None
        return round(result[1], 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose how the value was derived, so its resolution is visible."""
        result = self._latest_interval()
        if result is None:
            return None

        end_time, _, interval = result
        return {
            "measurement_interval_seconds": round(interval.total_seconds()),
            "reading_timestamp": end_time.isoformat(),
        }


class ObiCurrentPowerSensor(ObiPowerSensorBase):
    """Sensor for current power drawn from the grid."""

    _attr_unique_id = "obi_current_power"
    _attr_translation_key = "current_power"
    _measure = "energy"


class ObiCurrentFeedInPowerSensor(ObiPowerSensorBase):
    """Sensor for current power fed into the grid."""

    _attr_unique_id = "obi_current_feed_in_power"
    _attr_translation_key = "current_feed_in_power"
    _measure = "negative_energy"


class ObiLivePowerSensor(SensorEntity):
    """Momentary power from the live websocket.

    Unlike the derived power sensors this is a real measurement, but it only
    has a value while the live mode switch is on.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_unique_id = "obi_live_power"
    _attr_translation_key = "live_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "W"
    _attr_suggested_display_precision = 0

    def __init__(self, live: ObiLiveMode) -> None:
        """Initialize the sensor."""
        self._live = live
        self._attr_device_info = {
            "identifiers": {(DOMAIN, "obi_energy_tracker")},
            "name": "Obi EnergyTracker",
            "manufacturer": "Obi",
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to live mode updates."""
        self.async_on_remove(self._live.async_add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        """Write the new state when a frame arrives."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Only available while the websocket actually delivers data."""
        return self._live.connected

    @property
    def native_value(self) -> float | None:
        """Return the most recent live power value in watts."""
        return self._live.power
