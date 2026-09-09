"""Only bundled drivers can be selected by configuration."""

from .drivers.eightbitdo_ultimate2 import Ultimate2Driver
from .types import InputDriver

DRIVERS: tuple[InputDriver, ...] = (Ultimate2Driver(),)


def get_driver(driver_id: str) -> InputDriver:
    for driver in DRIVERS:
        if driver.id == driver_id:
            return driver
    raise ValueError(f"Unknown input driver: {driver_id}")
