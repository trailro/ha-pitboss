from collections.abc import Awaitable, Callable

import pytest
from conftest import get_entity
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pitboss.binary_sensor import (
    ENTITY_DESCRIPTIONS,
    PROBE_ERROR_KEYS,
    BinarySensor,
)
from custom_components.pitboss.const import DOMAIN, probe_label
from custom_components.pitboss.coordinator import PitBossDataUpdateCoordinator

pytestmark = pytest.mark.parametrize("model", ["PBV4PS2"])


def _entity_id(name: str) -> str:
    return f"binary_sensor.mygrill_{name.lower().replace(' ', '_')}"


async def test_creates_all_entities(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
) -> None:
    await mock_add_config_entry()
    for description in ENTITY_DESCRIPTIONS:
        assert isinstance(description.name, str)
        name = description.name
        # The probe error sensors are renamed after the physical port.
        if (probe_number := PROBE_ERROR_KEYS.get(description.key)) is not None:
            name = f"{probe_label(True, probe_number)} error"
        entity_id = _entity_id(name)
        assert hass.states.get(entity_id) is not None, entity_id


async def test_is_on_no_data(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
) -> None:
    await mock_add_config_entry()
    state = hass.states.get(_entity_id("MPC error"))
    assert state is not None
    assert state.state == "unavailable"

    entity = get_entity(hass, "binary_sensor", _entity_id("MPC error"), BinarySensor)
    assert entity.is_on is None


async def test_is_on_with_data(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
) -> None:
    entry = await mock_add_config_entry()
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_set_updated_data({"err1": True, "motorState": False})
    await hass.async_block_till_done()
    probe_1_error = hass.states.get(_entity_id("MPC error"))
    auger = hass.states.get(_entity_id("Auger"))
    assert probe_1_error is not None
    assert auger is not None
    assert probe_1_error.state == "on"
    assert auger.state == "off"


async def test_fan_and_igniter_report_their_state(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
) -> None:
    """Both are already decoded by pytboss; they just were not exposed."""
    entry = await mock_add_config_entry()
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_set_updated_data({"fanState": True, "hotState": False})
    await hass.async_block_till_done()

    fan = hass.states.get(_entity_id("Fan"))
    igniter = hass.states.get(_entity_id("Igniter"))
    assert fan is not None
    assert igniter is not None
    assert fan.state == "on"
    assert igniter.state == "off"


async def test_target_reached_flips_when_the_probe_gets_there(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
) -> None:
    entry = await mock_add_config_entry()
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.probe_targets[1] = 165

    coordinator.async_set_updated_data({"p1Temp": 150})
    await hass.async_block_till_done()
    state = hass.states.get(_entity_id("MPC target reached"))
    assert state is not None
    assert state.state == "off"

    coordinator.async_set_updated_data({"p1Temp": 165})
    await hass.async_block_till_done()
    state = hass.states.get(_entity_id("MPC target reached"))
    assert state is not None
    assert state.state == "on"


async def test_target_reached_is_off_rather_than_unknown(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
) -> None:
    """No probe and no target still means the target is not reached."""
    entry = await mock_add_config_entry()
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_set_updated_data({"moduleIsOn": False})
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("MPC target reached"))
    assert state is not None
    assert state.state == "off"
