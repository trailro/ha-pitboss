from collections.abc import Awaitable, Callable
from unittest.mock import Mock

import pytest
from conftest import get_entity
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pitboss.const import DOMAIN
from custom_components.pitboss.coordinator import PitBossDataUpdateCoordinator
from custom_components.pitboss.number import TargetProbeTemperature


@pytest.mark.parametrize("model", ["PBV4PS2"])
async def test_an_entity_per_probe(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
) -> None:
    """Every probe gets a target, whether or not the board has a command.

    This grill declares `set-probe-1-temperature` only; P2's target goes to
    the scratchpad instead. It has two probes, so there is no P3 or P4.
    """
    await mock_add_config_entry()
    assert hass.states.get("number.mygrill_mpc_target") is not None
    assert hass.states.get("number.mygrill_p2_target") is not None
    assert hass.states.get("number.mygrill_p3_target") is None


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
async def test_probes_without_any_command_still_get_targets(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
) -> None:
    """This grill declares no probe command at all, and has four probes.

    Before the scratchpad route it got no target entities whatsoever.
    """
    await mock_add_config_entry()
    for probe_number in (1, 2, 3, 4):
        entity_id = f"number.mygrill_probe_{probe_number}_target"
        assert hass.states.get(entity_id) is not None, entity_id


@pytest.mark.parametrize("model", ["PBV4PS2"])
async def test_native_value_and_availability(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
) -> None:
    entry = await mock_add_config_entry()
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Available with no probe plugged in: a target can be dialled in before
    # the probe goes into the meat.
    coordinator.async_set_updated_data({"p1Target": 165, "isFahrenheit": True})
    await hass.async_block_till_done()
    state = hass.states.get("number.mygrill_mpc_target")
    assert state is not None
    assert state.state == "165"

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


@pytest.mark.parametrize("model", ["PBV4PS2"])
async def test_a_probe_without_a_command_goes_to_the_scratchpad(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
    mock_pitboss: Mock,
) -> None:
    """P2 has no board command here, so its target is written to vData."""
    entry = await mock_add_config_entry()
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.virtual_data = {"p1T": 200, "psw": "secret"}
    coordinator.async_set_updated_data({"moduleIsOn": True, "isFahrenheit": True})
    await hass.async_block_till_done()

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.mygrill_p2_target", "value": 165},
        blocking=True,
    )

    # The firmware assigns the payload wholesale, so untouched keys have to be
    # sent back -- and `psw` must not be stored as data.
    mock_pitboss.set_virtual_data.assert_awaited_once_with({"p1T": 200, "p2T": 165})
    mock_pitboss.set_probe_2_temperature.assert_not_awaited()
    state = hass.states.get("number.mygrill_p2_target")
    assert state is not None
    assert state.state == "165"


@pytest.mark.parametrize("model", ["PBV4PS2"])
async def test_the_scratchpad_is_written_in_fahrenheit(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
    mock_pitboss: Mock,
) -> None:
    """vData is always Fahrenheit, whatever unit the grill is working in.

    Driven through the coordinator rather than the entity: Home Assistant
    fixes an entity's display unit when it is registered, so going through the
    service would measure its conversion on top of ours.
    """
    entry = await mock_add_config_entry()
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_set_updated_data({"moduleIsOn": True, "isFahrenheit": False})
    await hass.async_block_till_done()

    await coordinator.async_set_probe_target(2, 74)

    mock_pitboss.set_virtual_data.assert_awaited_once_with({"p2T": 165})
    # ...and reads back in the grill's unit rather than as 165.
    assert coordinator.probe_target(2) == 74


@pytest.mark.parametrize("model", ["PBV4PS2"])
async def test_a_target_set_while_off_is_kept_not_written(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
    mock_pitboss: Mock,
) -> None:
    """The scratchpad only accepts writes while the grill is on."""
    entry = await mock_add_config_entry()
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_set_updated_data({"moduleIsOn": False, "isFahrenheit": True})
    await hass.async_block_till_done()

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.mygrill_p2_target", "value": 165},
        blocking=True,
    )

    mock_pitboss.set_virtual_data.assert_not_awaited()
    # Still shown, so the user sees what will be sent at power-on.
    state = hass.states.get("number.mygrill_p2_target")
    assert state is not None
    assert state.state == "165"


@pytest.mark.parametrize("model", ["PBV4PS2"])
async def test_a_reported_target_wins_over_the_scratchpad(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
) -> None:
    """The board reports `p1Target` itself; vData must not override it."""
    entry = await mock_add_config_entry()
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.virtual_data = {"p1T": 200}
    coordinator.async_set_updated_data(
        {"p1Target": 165, "moduleIsOn": True, "isFahrenheit": True}
    )
    await hass.async_block_till_done()

    state = hass.states.get("number.mygrill_mpc_target")
    assert state is not None
    assert state.state == "165"


@pytest.mark.parametrize("model", ["PBV4PS2"])
async def test_a_target_set_while_off_is_written_at_power_on(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
    mock_pitboss: Mock,
) -> None:
    """What makes setting a target on a cold grill mean anything."""
    entry = await mock_add_config_entry()
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_set_updated_data({"moduleIsOn": False, "isFahrenheit": True})
    await coordinator.async_set_probe_target(2, 165)
    mock_pitboss.set_virtual_data.assert_not_awaited()

    await coordinator._async_refresh_virtual_data({"moduleIsOn": True})

    mock_pitboss.set_virtual_data.assert_awaited_once_with({"p2T": 165})


@pytest.mark.parametrize("model", ["PBV4PS2"])
async def test_the_scratchpad_is_seeded_only_once_per_power_cycle(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
    mock_pitboss: Mock,
) -> None:
    entry = await mock_add_config_entry()
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_set_updated_data({"moduleIsOn": False, "isFahrenheit": True})
    await coordinator.async_set_probe_target(2, 165)

    for _ in range(3):
        await coordinator._async_refresh_virtual_data({"moduleIsOn": True})
    assert mock_pitboss.set_virtual_data.await_count == 1

    # Power cycle: the firmware wipes it, so it has to be seeded again.
    await coordinator._async_refresh_virtual_data({"moduleIsOn": False})
    await coordinator._async_refresh_virtual_data({"moduleIsOn": True})
    assert mock_pitboss.set_virtual_data.await_count == 2


@pytest.mark.parametrize("model", ["PBV4PS2"])
async def test_a_target_already_in_the_scratchpad_is_not_overwritten(
    hass: HomeAssistant,
    mock_add_config_entry: Callable[[], Awaitable[MockConfigEntry]],
    mock_pitboss: Mock,
) -> None:
    """One set from the vendor's app wins over the one we are holding."""
    entry = await mock_add_config_entry()
    coordinator: PitBossDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_set_updated_data({"moduleIsOn": False, "isFahrenheit": True})
    await coordinator.async_set_probe_target(2, 165)
    mock_pitboss.get_virtual_data.return_value = {"p2T": 190}

    await coordinator._async_refresh_virtual_data({"moduleIsOn": True})

    mock_pitboss.set_virtual_data.assert_not_awaited()
    assert coordinator.probe_target(2) == 190
