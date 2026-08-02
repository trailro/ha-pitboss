"""Number platform for pitboss."""

from dataclasses import dataclass
from typing import Literal

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.components.number.const import NumberDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    DEFAULT_PROBE_CELSIUS_STEP,
    DEFAULT_PROBE_FAHRENHEIT_STEP,
    DEFAULT_PROBE_MAX_TEMP,
    DEFAULT_PROBE_MIN_TEMP,
    DOMAIN,
    probe_label,
)
from .coordinator import PitBossDataUpdateCoordinator
from .entity import BaseEntity


@dataclass(frozen=True, kw_only=True)
class PitBossNumberEntityDescription(NumberEntityDescription):
    key: Literal["p1Target", "p2Target", "p3Target", "p4Target"]
    probe_number: Literal[1, 2, 3, 4]
    device_class: NumberDeviceClass = NumberDeviceClass.TEMPERATURE
    icon: str = "mdi:thermometer"
    matching_probe_key: Literal["p1Temp", "p2Temp", "p3Temp", "p4Temp"]


PROBE_DESCRIPTIONS = tuple(
    PitBossNumberEntityDescription(
        key=f"p{n}Target",  # type: ignore[arg-type]
        name=f"Probe {n} Target",
        probe_number=n,  # type: ignore[arg-type]
        matching_probe_key=f"p{n}Temp",  # type: ignore[arg-type]
    )
    for n in (1, 2, 3, 4)
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_devices: AddEntitiesCallback
):
    """Setup number platformm."""
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    assert entry.unique_id is not None
    probe_count = coordinator.api.spec.meat_probes or 0
    entities = [
        TargetProbeTemperature(coordinator, entry.unique_id, description)
        for description in PROBE_DESCRIPTIONS
        if description.probe_number <= probe_count
    ]
    if entities:
        async_add_devices(entities)


class TargetProbeTemperature(BaseEntity, NumberEntity):
    """PitBoss target probe temperature class."""

    def __init__(
        self,
        coordinator: PitBossDataUpdateCoordinator,
        entry_unique_id: str,
        entity_description: PitBossNumberEntityDescription,
    ) -> None:
        super().__init__(coordinator, entry_unique_id)
        self.entity_description: PitBossNumberEntityDescription = entity_description
        self._attr_unique_id = f"{entity_description.key}_{entry_unique_id}"
        label = probe_label(coordinator.has_mpc, entity_description.probe_number)
        self._attr_name = f"{label} target"

    @property
    def native_unit_of_measurement(self) -> UnitOfTemperature | None:
        """Return the unit of measurement of the entity."""
        if (data := self.coordinator.data) and not data.get("isFahrenheit"):
            return UnitOfTemperature.CELSIUS
        return UnitOfTemperature.FAHRENHEIT

    @property
    def native_step(self) -> float:
        """Return the step size of the number."""
        if self.native_unit_of_measurement == UnitOfTemperature.FAHRENHEIT:
            return DEFAULT_PROBE_FAHRENHEIT_STEP
        else:
            return DEFAULT_PROBE_CELSIUS_STEP

    @property
    def available(self) -> bool:
        if data := self.coordinator.data:
            return (
                data.get(self.entity_description.matching_probe_key) is not None
                and super().available
            )
        return super().available

    @property
    def native_value(self) -> int | None:
        """Return the native value of the probe target."""
        return self.coordinator.probe_target(self.entity_description.probe_number)

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        await self.coordinator.async_set_probe_target(
            self.entity_description.probe_number, int(value)
        )
        self.coordinator.async_update_listeners()

    @property
    def native_min_value(self) -> float:
        """Return the minimum value."""
        min_temp = DEFAULT_PROBE_MIN_TEMP
        return TemperatureConverter.convert(
            min_temp, UnitOfTemperature.FAHRENHEIT, self.native_unit_of_measurement
        )

    @property
    def native_max_value(self) -> float:
        """Return the maximum value."""
        max_temp = DEFAULT_PROBE_MAX_TEMP
        return TemperatureConverter.convert(
            max_temp, UnitOfTemperature.FAHRENHEIT, self.native_unit_of_measurement
        )
