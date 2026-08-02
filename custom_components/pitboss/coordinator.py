"""DataUpdateCoordinator for PitBoss."""

from math import floor

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pytboss.api import PitBoss
from pytboss.exceptions import GrillUnavailable, NotConnectedError, RPCError
from pytboss.grills import StateDict

from .const import DOMAIN, LOGGER, PING_INTERVAL


class PitBossDataUpdateCoordinator(DataUpdateCoordinator[StateDict]):
    """Class to manage fetching data from the API."""

    config_entry: ConfigEntry
    device_info: DeviceInfo
    api: PitBoss

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
        # Targets for probes the control board cannot hold. Kept here so they
        # survive the grill being off, when the scratchpad below is wiped.
        self.probe_targets: dict[int, int] = {}
        # The grill's scratchpad. The official app stores the targets the
        # board has no command for in here, so reading it is how a target set
        # from the phone shows up. Always Fahrenheit.
        self.virtual_data: dict = {}
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
        """Whether the grill has a meat probe control port.

        Read off the raw definition rather than `spec.has_mpc`, which only
        exists on pytboss newer than the pinned 2026.8.1.
        """
        return bool(self.api.spec.json.get("has_mpc"))

    def probe_command(self, probe_number: int) -> str | None:
        """The board command that sets this probe's target, if it has one.

        Only probes 1 and 2 ever have one, and only on some boards: across
        the whole vendor database no board declares a command for probe 3 or
        4. Where there is none, the target lives in the scratchpad instead.
        """
        slug = f"set-probe-{probe_number}-temperature"
        if slug in self.api.spec.control_board.commands:
            return slug
        return None

    def probe_target(self, probe_number: int) -> int | None:
        """The target set for a probe, in the grill's own unit.

        Three sources, most authoritative first: what the board reports, what
        the scratchpad holds -- which is how a target set from the phone app
        appears here -- and finally the last value we sent, which is all that
        is left while the grill is off and the scratchpad is empty.
        """
        if (data := self.data) and (
            reported := data.get(f"p{probe_number}Target")
        ) is not None:
            return int(reported)  # type: ignore[call-overload]
        if (raw := self.virtual_data.get(f"p{probe_number}T")) is not None:
            return self._from_fahrenheit(raw)
        return self.probe_targets.get(probe_number)

    def _grill_is_fahrenheit(self) -> bool:
        return bool(self.data and self.data.get("isFahrenheit"))

    def _from_fahrenheit(self, value: float) -> int:
        if self._grill_is_fahrenheit():
            return int(value)
        return floor((value - 32) / 1.8)

    def _to_fahrenheit(self, value: int) -> int:
        if self._grill_is_fahrenheit():
            return value
        return round(value * 1.8 + 32)

    async def async_set_probe_target(self, probe_number: int, temp: int) -> None:
        """Set a probe's target, by whichever route this probe supports."""
        if self.probe_command(probe_number):
            setter = (
                self.api.set_probe_temperature
                if probe_number == 1
                else self.api.set_probe_2_temperature
            )
            await setter(temp)
        elif self.data and self.data.get("moduleIsOn"):
            # The scratchpad only accepts writes while the grill is on; when
            # it is off the value stays with us and is pushed at power-on.
            await self._async_write_virtual_data(
                {f"p{probe_number}T": self._to_fahrenheit(temp)}
            )
        self.probe_targets[probe_number] = temp

    async def _async_write_virtual_data(self, updates: dict) -> None:
        """Merge `updates` into the scratchpad.

        The firmware assigns the payload wholesale, so anything not sent back
        is lost; the official app merges client-side for the same reason.
        """
        payload = {k: v for k, v in self.virtual_data.items() if k != "psw"}
        payload.update(updates)
        await self.api.set_virtual_data(payload)
        self.virtual_data = payload

    async def _async_refresh_virtual_data(self, state: StateDict) -> None:
        """Track the scratchpad. Never fatal."""
        if not state.get("moduleIsOn"):
            # The firmware clears it on every status frame while off.
            self.virtual_data = {}
            self._vdata_seeded = False
            return
        try:
            data = await self.api.get_virtual_data()
        except Exception as ex:  # noqa: BLE001
            self.logger.debug("Could not read the scratchpad: %s", ex)
            return
        self.virtual_data = data if isinstance(data, dict) else {}
        # Adopt whatever it holds, so a target set from the phone survives the
        # grill being switched off and the scratchpad being wiped.
        for probe_number in range(1, 5):
            if self.probe_command(probe_number):
                continue
            if (raw := self.virtual_data.get(f"p{probe_number}T")) is not None:
                self.probe_targets[probe_number] = self._from_fahrenheit(raw)
        if self._vdata_seeded:
            return
        self._vdata_seeded = True
        # It came on with an empty scratchpad: hand it what we are holding so
        # the phone app sees the same targets. Anything already there was set
        # from the app and wins.
        updates = {
            f"p{n}T": self._to_fahrenheit(temp)
            for n, temp in self.probe_targets.items()
            if not self.probe_command(n) and f"p{n}T" not in self.virtual_data
        }
        if not updates:
            return
        try:
            await self._async_write_virtual_data(updates)
        except Exception as ex:  # noqa: BLE001
            self.logger.debug("Could not seed the scratchpad: %s", ex)

    async def _async_setup(self) -> None:
        """Set up the coordinator."""
        await self.api.subscribe_state(self._on_state_update)
        await self._start_api()

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

        # Always fetch the current state to ensure sensors stay up-to-date.
        # Relying solely on push notifications means sensors can go stale after
        # a reconnect if push notifications stop being delivered.
        try:
            merged = self._merge_state(await self.api.get_state())
            await self._async_refresh_virtual_data(merged)
            return merged
        except NotConnectedError as ex:
            raise UpdateFailed("Grill not connected") from ex
        except RPCError as ex:
            raise UpdateFailed(str(ex)) from ex
