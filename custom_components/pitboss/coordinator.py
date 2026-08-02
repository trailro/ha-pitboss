"""DataUpdateCoordinator for PitBoss."""

from math import floor
from time import monotonic

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.unit_conversion import TemperatureConverter
from pytboss.api import PitBoss
from pytboss.exceptions import GrillUnavailable, NotConnectedError, RPCError
from pytboss.grills import StateDict

from .const import DOMAIN, LOGGER, PING_INTERVAL, SYS_INFO_INTERVAL


class PitBossDataUpdateCoordinator(DataUpdateCoordinator[StateDict]):
    """Class to manage fetching data from the API."""

    config_entry: ConfigEntry
    device_info: DeviceInfo
    api: PitBoss
    firmware_version: str | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        device_info: DeviceInfo,
        api: PitBoss,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass=hass, logger=LOGGER, name=DOMAIN, update_interval=PING_INTERVAL
        )
        self.device_info = device_info
        self.api = api
        self._api_started = False
        # Latest Sys.GetInfo payload from the control board.
        self.sys_info: dict = {}
        self._sys_info_at = 0.0
        # The grill's scratchpad, and our own copy of the targets we set
        # through it. See `async_set_probe_target`.
        self.virtual_data: dict = {}
        self.probe_targets: dict[int, int] = {}
        self._vdata_seeded = False

    def accepted_setpoints(self, unit: str) -> list[float]:
        """Grill setpoints the control board honours, expressed in `unit`.

        The board ignores anything that is not on this list. A couple of
        models publish a Celsius list of their own; for everyone else it is
        derived from the Fahrenheit one using the same conversion the boards
        that convert in their own parsing routine use -- `floor((F - 32) /
        1.8)` -- so the values match what the panel will show.
        """
        fahrenheit = self.api.spec.temp_increments or []
        if unit != UnitOfTemperature.CELSIUS:
            return [float(v) for v in fahrenheit]
        raw = self.api.spec.json.get("celsius_temp_increment") or ""
        if celsius := [int(v) for v in raw.split("/") if v.strip().isdigit()]:
            return [float(v) for v in celsius]
        return [float(floor((v - 32) / 1.8)) for v in fahrenheit]

    @property
    def has_mpc(self) -> bool:
        """Whether the grill has a meat probe control port."""
        return self.api.spec.has_mpc

    @property
    def grill_unit(self) -> str:
        """The unit the grill is currently working in."""
        if (data := self.data) and not data.get("isFahrenheit"):
            return UnitOfTemperature.CELSIUS
        return UnitOfTemperature.FAHRENHEIT

    def probe_command_slug(self, probe_number: int) -> str | None:
        """The board command that sets this probe's target, when it exists.

        No board in the catalogue declares one for probes 3 or 4, and only 42
        of 137 declare one for probe 1, so most probes have no command route
        at all.
        """
        slug = f"set-probe-{probe_number}-temperature"
        if slug in self.api.spec.control_board.commands:
            return slug
        return None

    def probe_target(self, probe_number: int) -> int | None:
        """This probe's target, in the grill's own unit.

        The board reports a target of its own only for the control probe --
        `p1Target` on every model, `p2Target` on 26 of them -- so for the rest
        the scratchpad, and failing that our own copy, is the only source.
        """
        if (data := self.data) is not None:
            reported = data.get(f"p{probe_number}Target")
            if isinstance(reported, (int, float)):
                return int(reported)
        raw = self.virtual_data.get(f"p{probe_number}T")
        if raw is not None:
            return round(self.temperature_from_fahrenheit(raw))
        return self.probe_targets.get(probe_number)

    def temperature_from_fahrenheit(self, value: float) -> float:
        """Convert a scratchpad temperature into the grill's own unit."""
        if self.grill_unit == UnitOfTemperature.FAHRENHEIT:
            return float(value)
        return TemperatureConverter.convert(
            float(value), UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
        )

    def temperature_to_fahrenheit(self, value: float) -> int:
        """Convert a grill-unit temperature into what the scratchpad wants."""
        if self.grill_unit == UnitOfTemperature.FAHRENHEIT:
            return round(value)
        return round(
            TemperatureConverter.convert(
                value, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
            )
        )

    async def _async_write_virtual_data(self, updates: dict) -> None:
        """Merge `updates` into the grill's scratchpad.

        The firmware assigns the payload wholesale (`vData = params`), so
        anything not sent back is lost -- the vendor's app merges client-side
        too. `psw` is dropped because authentication injects it into the same
        object and it would otherwise be stored as data.
        """
        payload = {k: v for k, v in self.virtual_data.items() if k != "psw"}
        payload.update(updates)
        await self.api.set_virtual_data(payload)
        self.virtual_data = payload

    async def async_set_probe_target(self, probe_number: int, temp: int) -> None:
        """Set a probe's target, by whichever route the grill supports."""
        if self.probe_command_slug(probe_number) is not None:
            if probe_number == 1:
                await self.api.set_probe_temperature(temp)
            else:
                await self.api.set_probe_2_temperature(temp)
        elif (data := self.data) and data.get("moduleIsOn"):
            # The scratchpad only accepts writes while the grill is on; when
            # it is not, the value stays with us and is written at power-on.
            await self._async_write_virtual_data(
                {f"p{probe_number}T": self.temperature_to_fahrenheit(temp)}
            )
        self.probe_targets[probe_number] = temp

    async def _async_refresh_virtual_data(self, state: StateDict) -> None:
        """Track the grill's scratchpad. Never fatal."""
        if not state.get("moduleIsOn"):
            # The firmware clears it on every status frame while off.
            self.virtual_data = {}
            self._vdata_seeded = False
            return
        try:
            data = await self.api.get_virtual_data()
        except Exception as ex:  # noqa: BLE001
            self.logger.debug("Could not fetch the virtual data: %s", ex)
            return
        self.virtual_data = data if isinstance(data, dict) else {}
        # Adopt whatever the scratchpad holds as our own last-known value, so a
        # target set from the vendor's app does not snap back to ours when the
        # grill goes off and the firmware wipes the scratchpad.
        for number in range(1, (self.api.spec.meat_probes or 1) + 1):
            if self.probe_command_slug(number) is not None:
                continue
            raw = self.virtual_data.get(f"p{number}T")
            if raw is not None:
                self.probe_targets[number] = round(
                    self.temperature_from_fahrenheit(raw)
                )

        if self._vdata_seeded:
            return
        self._vdata_seeded = True
        # The grill has just come on with an empty scratchpad. Hand it the
        # targets we are holding, which is what makes setting one while the
        # grill is off mean anything. A value already there was set from the
        # vendor's app and wins.
        updates = {
            f"p{number}T": self.temperature_to_fahrenheit(temp)
            for number, temp in self.probe_targets.items()
            if self.probe_command_slug(number) is None
            and f"p{number}T" not in self.virtual_data
        }
        if not updates:
            return
        try:
            await self._async_write_virtual_data(updates)
        except Exception as ex:  # noqa: BLE001
            self.logger.debug("Could not seed the virtual data: %s", ex)

    async def _async_setup(self) -> None:
        """Set up the coordinator."""
        await self.api.subscribe_state(self._on_state_update)
        await self._start_api()
        try:
            result = await self.api.get_firmware_version()
            self.firmware_version = result.get("firmwareVersion")
        except Exception as ex:  # noqa: BLE001
            # Cosmetic; never worth failing setup over.
            self.logger.debug("Could not fetch the firmware version: %s", ex)

    def _merge_state(self, state: StateDict) -> StateDict:
        """Fold a state frame onto the last known one.

        The board answers with two independent frames, status (`sc_11`) and
        temperatures (`sc_12`), and clears them as soon as it forwards a
        command to the MCU. A read landing in that window returns one frame
        without the other, and pytboss simply omits the missing half's keys,
        so every entity backed by them would go unknown until the next
        successful read.

        Only *absent* keys are carried over. A key present with a null value
        is a real reading -- an unplugged probe -- and overwrites.
        """
        # Copy rather than hand back the incoming dict: pytboss gives every
        # subscriber the same StateDict instance and keeps mutating it.
        if not self.data:
            return state.copy()
        if not state:
            return self.data
        merged = self.data.copy()
        merged.update(state)
        return merged

    async def _on_state_update(self, data: StateDict) -> None:
        self.logger.debug("Received data: %s", data)
        self.async_set_updated_data(self._merge_state(data))

    async def _start_api(self) -> None:
        try:
            await self.api.start()
            self._api_started = True
        except GrillUnavailable as ex:
            raise UpdateFailed("Grill unavailable") from ex

    async def _async_refresh_sys_info(self) -> None:
        """Refresh the control board's system info. Never fatal.

        Uptime and free memory change slowly and are diagnostic, so they are
        not worth a round trip on every poll.
        """
        now = monotonic()
        if self.sys_info and now - self._sys_info_at < SYS_INFO_INTERVAL:
            return
        try:
            self.sys_info = await self.api.config.get_info()
            self._sys_info_at = now
        except Exception as ex:  # noqa: BLE001
            self.logger.debug("Could not fetch the system info: %s", ex)

    async def _async_update_data(self) -> StateDict:
        if not self._api_started:
            self.logger.debug("Starting API")
            await self._start_api()

        if not self.api.is_connected():
            raise UpdateFailed("Grill not connected")

        try:
            await self.api.ping(timeout=10.0)
        except NotConnectedError as ex:
            raise UpdateFailed("Grill not connected") from ex

        await self._async_refresh_sys_info()

        # Always fetch the current state to ensure sensors stay up-to-date.
        # Relying solely on push notifications means sensors can go stale after
        # a reconnect if push notifications stop being delivered.
        try:
            state = self._merge_state(await self.api.get_state())
            await self._async_refresh_virtual_data(state)
            return state
        except NotConnectedError as ex:
            raise UpdateFailed("Grill not connected") from ex
        except RPCError as ex:
            raise UpdateFailed(str(ex)) from ex
