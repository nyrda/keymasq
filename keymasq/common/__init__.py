from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING

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

def __getattr__(name: str) -> ModuleType:
    if name in __all__:
        module = import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
