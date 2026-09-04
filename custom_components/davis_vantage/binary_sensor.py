"""Binary sensor platform for Davis Vantage."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    DOMAIN as BINARY_SENSOR_DOMAIN,
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_NAME
from .coordinator import DavisVantageDataUpdateCoordinator
from .utils import normalize_unique_id

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class DavisBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Class describing Davis Vantage binary sensor entities."""

    value_fn: Callable[[dict[str, Any]], bool | None]


def _evaluate_is_raining(data: dict[str, Any]) -> bool:
    """Safely determine if it is raining."""
    return bool(data.get("IsRaining"))


def _evaluate_tx_battery(data: dict[str, Any]) -> bool:
    """Safely check transmitter battery status (1 = low battery)."""
    # LOOP1 byte 86: 1 = low battery, 0 = normal.
    return bool(data.get("TransmitterBatteryStatus"))


def _evaluate_iss_connection(data: dict[str, Any]) -> bool:
    """Safely check if the sensor suite is online based on valid telemetry."""
    temp = data.get("TempOut")
    wind = data.get("WindSpeed")
    
    # 255, 32767 and -32768 are Davis dash values.
    temp_valid = temp is not None and temp not in (255, 32767, 32768, -32768, "")
    wind_valid = wind is not None and wind not in (255, 32767, 32768, -32768, "")
    
    return bool(temp_valid or wind_valid)


BINARY_SENSOR_TYPES: tuple[DavisBinarySensorEntityDescription, ...] = (
    DavisBinarySensorEntityDescription(
        key="is_raining",
        translation_key="is_raining",
        icon="mdi:weather-rainy",
        device_class=BinarySensorDeviceClass.MOISTURE,
        value_fn=_evaluate_is_raining,
    ),

    DavisBinarySensorEntityDescription(
        key="flash_flood_alarm",
        translation_key="flash_flood_alarm",
        icon="mdi:home-flood",
        device_class=BinarySensorDeviceClass.SAFETY,
        value_fn=lambda data: bool(data.get("FlashFloodAlarm")),
    ),
    DavisBinarySensorEntityDescription(
        key="storm_rain_alarm",
        translation_key="storm_rain_alarm",
        icon="mdi:weather-pouring",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda data: bool(data.get("StormRainAlarm")),
    ),
    DavisBinarySensorEntityDescription(
        key="thsw_alarm",
        translation_key="thsw_alarm",
        icon="mdi:sun-thermometer",
        device_class=BinarySensorDeviceClass.SAFETY,
        value_fn=lambda data: bool(data.get("THSWAlarm")),
    ),

    DavisBinarySensorEntityDescription(
        key="tx_battery_status",
        translation_key="tx_battery_status",
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_evaluate_tx_battery,
    ),
    DavisBinarySensorEntityDescription(
        key="iss_connection",
        translation_key="iss_connection",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_evaluate_iss_connection,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Davis Vantage sensors based on a config entry."""
    coordinator = config_entry.runtime_data.coordinator
    entity_registry = async_get_entity_registry(hass)

    for desc in BINARY_SENSOR_TYPES:
        if desc.key:
            old_unique_id = f"{config_entry.entry_id}-{DEFAULT_NAME} {desc.key}"
            new_unique_id = normalize_unique_id(old_unique_id)
            entity = entity_registry.async_get_entity_id(
                BINARY_SENSOR_DOMAIN, config_entry.domain, old_unique_id
            )
            if entity and not entity_registry.async_get_entity_id(
                BINARY_SENSOR_DOMAIN, config_entry.domain, new_unique_id
            ):
                entity_registry.async_update_entity(entity, new_unique_id=new_unique_id)

    entities: list[DavisVantageBinarySensor] = []

    for description in BINARY_SENSOR_TYPES:
        entities.append(
            DavisVantageBinarySensor(
                coordinator=coordinator,
                entry_id=config_entry.entry_id,
                description=description,
            )
        )

    async_add_entities(entities)


class DavisVantageBinarySensor(
    CoordinatorEntity[DavisVantageDataUpdateCoordinator], BinarySensorEntity
):
    """Defines a Davis Vantage binary sensor."""

    _attr_has_entity_name = True
    entity_description: DavisBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: DavisVantageDataUpdateCoordinator,
        entry_id: str,
        description: DavisBinarySensorEntityDescription,
    ) -> None:
        """Initialize Davis Vantage sensor."""
        super().__init__(coordinator=coordinator)
        self.entity_description = description
        
        self._attr_unique_id = normalize_unique_id(
            f"{entry_id}_{DEFAULT_NAME}_{description.key}"
        )
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool | None:
        """Return the state of the binary sensor."""
        if not self.coordinator.data:
            return None
            
        try:
            return self.entity_description.value_fn(self.coordinator.data)
        except (KeyError, TypeError, AttributeError):
            return False