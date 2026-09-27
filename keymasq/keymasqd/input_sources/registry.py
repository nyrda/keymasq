"""Only bundled drivers can be selected by configuration."""

from .drivers.eightbitdo_ultimate2 import Ultimate2Driver
from .drivers.steam_deck_touch import SteamDeckTouchDriver
from .types import InputDriver

DRIVERS: tuple[InputDriver, ...] = (Ultimate2Driver(), SteamDeckTouchDriver())
