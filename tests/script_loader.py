from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType


def load_script(
    path: Path,
    module_name: str,
    *,
    cleanup_modules: Iterable[str] = (),
) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    tracked_module_names = [module_name]
    for cleanup_module in cleanup_modules:
        if cleanup_module not in tracked_module_names:
            tracked_module_names.append(cleanup_module)

    original_path = list(sys.path)
    previous_modules: dict[str, ModuleType] = {}
    missing_modules: set[str] = set()
    for name in tracked_module_names:
        if name in sys.modules:
            previous_modules[name] = sys.modules[name]
        else:
            missing_modules.add(name)

    sys.path.insert(0, str(path.parent))
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_path
        for name in tracked_module_names:
            if name in missing_modules:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous_modules[name]

    return module
