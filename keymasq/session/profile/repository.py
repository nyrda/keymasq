import logging
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from keymasq.common import paths
from keymasq.common.config_files import write_toml_atomically
from keymasq.common.model.profiles import ProfileConfig
from keymasq.session.config_loading import load_config_files_sync

from .codec import ProfileCodec
from .types import ProfileInfo

log = logging.getLogger("keymasq-session.profiles")

MAX_PATH_ATTEMPTS = 10000


class ProfileRepository:
    """Own profile file discovery, naming, allocation, and atomic persistence."""

    def __init__(self, codec: ProfileCodec) -> None:
        self.codec = codec

    def load_all(
        self,
        *,
        strict: bool = False,
        on_created_at_repair: Callable[[ProfileConfig, Path, str], None] | None = None,
    ) -> dict[str, ProfileInfo]:
        def load_profile(path: Path) -> ProfileConfig:
            decoded = self.codec.load(path)
            if decoded.created_at_repair_reason is not None and on_created_at_repair is not None:
                on_created_at_repair(
                    decoded.config,
                    path,
                    decoded.created_at_repair_reason,
                )
            return decoded.config

        loaded_profiles: dict[str, ProfileInfo] = {}
        for profile_file, config in load_config_files_sync(
            paths.PROFILES_DIR,
            config_kind="profile",
            strict=strict,
            load_config=load_profile,
            logger=log,
            sort_paths=True,
        ):
            self._add_loaded(
                ProfileInfo(path=profile_file, config=config),
                loaded_profiles,
            )
        return loaded_profiles

    def _add_loaded(
        self,
        profile: ProfileInfo,
        profiles: dict[str, ProfileInfo],
    ) -> None:
        existing = profiles.get(profile.config.name)
        if existing is None:
            profiles[profile.config.name] = profile
            return

        selected = self._select_duplicate(existing, profile)
        ignored = profile if selected is existing else existing
        profiles[profile.config.name] = selected
        log.warning(
            "Ignoring duplicate profile name '%s' from %s; using %s",
            profile.config.name,
            ignored.path,
            selected.path,
        )

    def _select_duplicate(self, first: ProfileInfo, second: ProfileInfo) -> ProfileInfo:
        first_is_canonical = self.is_canonical(first.config.name, first.path)
        second_is_canonical = self.is_canonical(second.config.name, second.path)
        if first_is_canonical and not second_is_canonical:
            return first
        if second_is_canonical and not first_is_canonical:
            return second
        return first

    @staticmethod
    def sanitize_stem(profile_name: str) -> str:
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", profile_name).strip("._")
        return safe_name or "profile"

    def canonical_path(self, profile_name: str) -> Path:
        return paths.PROFILES_DIR / f"{self.sanitize_stem(profile_name)}.toml"

    def is_canonical(self, profile_name: str, path: Path) -> bool:
        return path == self.canonical_path(profile_name)

    def allocate_path(
        self,
        profile_name: str,
        *,
        current_path: Path | None = None,
        occupied_paths: set[Path] | None = None,
    ) -> Path:
        occupied_paths = occupied_paths or set()
        base_stem = self.sanitize_stem(profile_name)
        candidate = self.canonical_path(profile_name)
        suffix = 2
        attempts = 0
        while candidate in occupied_paths or (candidate.exists() and candidate != current_path):
            attempts += 1
            if attempts >= MAX_PATH_ATTEMPTS:
                raise RuntimeError(f"Unable to allocate profile storage path for '{profile_name}'")
            candidate = paths.PROFILES_DIR / f"{base_stem}_{suffix}.toml"
            suffix += 1
        return candidate

    def write(self, config: ProfileConfig, path: Path, *, exclusive: bool = False) -> None:
        write_toml_atomically(
            path,
            self.codec.encode(config),
            overwrite=not exclusive,
        )

    @staticmethod
    def trash(path: Path) -> None:
        trash_dir = paths.CONFIG_DIR / "trash" / "profiles"
        trash_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        trashed_path = trash_dir / f"{timestamp}_{path.name}"
        try:
            path.rename(trashed_path)
            log.warning("Moved deleted profile to trash: %s", trashed_path)
        except OSError as exc:
            log.warning(
                "Failed to move deleted profile to trash %s; deleting permanently: %s",
                path,
                exc,
            )
            try:
                path.unlink()
            except OSError as unlink_exc:
                log.warning(
                    "Failed to delete profile file %s after trash move failed: %s",
                    path,
                    unlink_exc,
                )
                raise
