from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from . import devices, ipc, models, paths, recording_guard, security, slurp

__all__ = [
    "devices",
    "ipc",
    "models",
    "paths",
    "recording_guard",
    "security",
    "slurp",
]

_MODULE_ORDER: Final[tuple[str, ...]] = (
    "paths",
    "recording_guard",
    "models",
    "ipc",
    "security",
    "slurp",
    "devices",
)
_LOADED_MODULES: dict[str, ModuleType] = {}


def _load_module(module_name: str) -> ModuleType:
    module = _LOADED_MODULES.get(module_name)
    if module is None:
        module = import_module(f"{__name__}.{module_name}")
        _LOADED_MODULES[module_name] = module
    return module


def __getattr__(name: str) -> object:
    if name in __all__:
        module = _load_module(name)
        globals()[name] = module
        return module

    for module_name in _MODULE_ORDER:
        module = _load_module(module_name)
        if hasattr(module, name):
            value = getattr(module, name)
            globals()[name] = value
            return value

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
