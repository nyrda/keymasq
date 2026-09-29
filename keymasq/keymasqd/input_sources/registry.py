"""Only bundled drivers can be selected by configuration."""

from .drivers.eightbitdo_ultimate2 import Ultimate2Driver
from .drivers.steam_deck_touch import SteamDeckTouchDriver
from .types import InputDriver

DRIVERS: tuple[InputDriver, ...] = (Ultimate2Driver(), SteamDeckTouchDriver())
MOTION_KINDS = frozenset({"gyro", "accelerometer"})


def supplies_motion(driver_id: object) -> bool:
    """Unknown drivers are left to their source binding, which rejects missing channels."""
    driver = next((item for item in DRIVERS if item.id == driver_id), None)
    return driver is None or any(channel.kind in MOTION_KINDS for channel in driver.channels)
