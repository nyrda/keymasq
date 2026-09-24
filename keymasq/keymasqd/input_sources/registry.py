"""Only bundled drivers can be selected by configuration."""

from .drivers.eightbitdo_ultimate2 import Ultimate2Driver
from .types import InputDriver

DRIVERS: tuple[InputDriver, ...] = (Ultimate2Driver(),)
