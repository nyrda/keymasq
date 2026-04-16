__all__ = [
    "ActionHandler",
    "HardwareManager",
    "KeymasqdClient",
    "ProfileInfo",
    "ProfileManager",
    "SessionManager",
]

from importlib import import_module
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .action_handler import ActionHandler
    from .client import KeymasqdClient
    from .hardware import HardwareManager
    from .manager import SessionManager
    from .profiles import ProfileInfo, ProfileManager

_SYMBOL_MODULES: Final[dict[str, str]] = {
    "ActionHandler": "action_handler",
    "HardwareManager": "hardware",
    "KeymasqdClient": "client",
    "ProfileInfo": "profiles",
    "ProfileManager": "profiles",
    "SessionManager": "manager",
}


def __getattr__(name: str) -> object:
    module_name = _SYMBOL_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = import_module(f"{__name__}.{module_name}")
    value = getattr(module, name)
    globals()[name] = value
    return value
