from collections.abc import Awaitable, Callable
from unittest.mock import Mock

import pytest
from conftest import get_entity
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import METRIC_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pitboss.const import DOMAIN
from custom_components.pitboss.coordinator import PitBossDataUpdateCoordinator
from custom_components.pitboss.number import TargetProbeTemperature


@pytest.mark.parametrize("model", ["PBV4PS2"])
async def test_target_for_every_probe(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
) -> None:
    """One target per probe, whether or not the board can hold it."""
    await mock_add_config_entry()
    assert hass.states.get("number.mygrill_mpc_target") is not None
    assert hass.states.get("number.mygrill_mp1_target") is not None


@pytest.mark.parametrize("model", ["PB1150PS3"])
async def test_both_probe_entities_created(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
) -> None:
    """This grill has no MPC, so the ports keep their plain numbering."""
    await mock_add_config_entry()
    assert hass.states.get("number.mygrill_probe_1_target") is not None
    assert hass.states.get("number.mygrill_probe_2_target") is not None


@pytest.mark.parametrize("model", ["PB2180LK"])
async def test_targets_exist_without_any_board_command(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
) -> None:
    """This board declares no probe command at all; all four use the scratchpad."""
    await mock_add_config_entry()
    for probe_number in range(1, 5):
        entity_id = f"number.mygrill_probe_{probe_number}_target"
        assert hass.states.get(entity_id) is not None, entity_id


@pytest.mark.parametrize("model", ["PBV4PS2"])
async def test_native_value_and_availability(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
) -> None:
    entry = await mock_add_config_entry()
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    # No matching probe temperature reported: entity unavailable.
    coordinator.async_set_updated_data({"p1Target": 165, "isFahrenheit": True})
    await hass.async_block_till_done()
    state = hass.states.get("number.mygrill_mpc_target")
    assert state is not None
    assert state.state == "unavailable"

    coordinator.async_set_updated_data(
        {"p1Target": 165, "p1Temp": 70, "isFahrenheit": True}
    )
    await hass.async_block_till_done()
    state = hass.states.get("number.mygrill_mpc_target")
    assert state is not None
    assert state.state == "165"
    assert state.attributes["unit_of_measurement"] == "°F"
    assert state.attributes["step"] == 1
    assert state.attributes["min"] == 50
    assert state.attributes["max"] == 250


@pytest.mark.parametrize("model", ["PBV4PS2"])
async def test_set_native_value(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
    mock_pitboss: Mock,
) -> None:
    entry = await mock_add_config_entry()
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_set_updated_data(
        {"p1Target": 165, "p1Temp": 70, "isFahrenheit": True}
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.mygrill_mpc_target", "value": 180},
        blocking=True,
    )
    mock_pitboss.set_probe_temperature.assert_awaited_once_with(180)


@pytest.mark.parametrize("model", ["PBV4PS2"])
async def test_native_value_none_without_data(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
) -> None:
    await mock_add_config_entry()
    entity = get_entity(
        hass, "number", "number.mygrill_mpc_target", TargetProbeTemperature
    )
    assert entity.native_value is None


@pytest.mark.parametrize("model", ["PB2180LK"])
async def test_target_without_a_board_command_goes_to_the_scratchpad(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
    mock_pitboss: Mock,
) -> None:
    entry = await mock_add_config_entry()
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_set_updated_data(
        {"moduleIsOn": True, "isFahrenheit": True, "p3Temp": 70}
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.mygrill_probe_3_target", "value": 165},
        blocking=True,
    )
    mock_pitboss.set_virtual_data.assert_awaited_once_with({"p3T": 165})


@pytest.mark.parametrize("model", ["PB2180LK"])
async def test_a_target_set_from_the_app_is_read_back(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
    mock_pitboss: Mock,
) -> None:
    """The scratchpad is how a target set on the phone reaches us."""
    entry = await mock_add_config_entry()
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    # The fixture resets get_state during setup, so set these afterwards.
    mock_pitboss.get_virtual_data.return_value = {"p3T": 165}
    mock_pitboss.get_state.return_value = {
        "moduleIsOn": True,
        "isFahrenheit": True,
        "p3Temp": 70,
    }
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get("number.mygrill_probe_3_target")
    assert state is not None
    assert state.state == "165"


@pytest.mark.parametrize("units", [METRIC_SYSTEM])
@pytest.mark.parametrize("model", ["PB2180LK"])
async def test_scratchpad_values_are_fahrenheit(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
    mock_pitboss: Mock,
) -> None:
    """It always holds fahrenheit, whatever unit the grill is working in."""
    entry = await mock_add_config_entry()
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_set_updated_data(
        {"moduleIsOn": True, "isFahrenheit": False, "p3Temp": 20}
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.mygrill_probe_3_target", "value": 74},
        blocking=True,
    )
    mock_pitboss.set_virtual_data.assert_awaited_once_with({"p3T": 165})
