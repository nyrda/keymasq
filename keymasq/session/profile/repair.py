import io
import tomllib
from datetime import datetime
from pathlib import Path
from typing import cast

from keymasq.common.config_files import write_toml_atomically

from .types import TomlDict


def repair_created_at(path: Path, created_at: datetime) -> None:
    """Repair only the timestamp, preserving any profile changes written meanwhile."""

    data = cast(TomlDict, tomllib.load(io.BytesIO(path.read_bytes())))
    profile_value = data.get("profile")
    profile = cast(TomlDict, profile_value) if isinstance(profile_value, dict) else None
    if profile is None:
        profile = {}
        data["profile"] = profile

    current_created_at = profile.get("created_at")
    if isinstance(current_created_at, str):
        try:
            datetime.fromisoformat(current_created_at)
        except ValueError:
            pass
        else:
            return

    profile["created_at"] = created_at.isoformat()
    write_toml_atomically(path, data)
