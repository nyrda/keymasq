import asyncio
import copy
import logging
import threading
import tomllib
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Concatenate

from keymasq.common import paths
from keymasq.common.model.profiles import (
    ProfileConfig,
    WindowRule,
)

from . import references
from .codec import ProfileCodec
from .repair import repair_created_at
from .repository import MAX_PATH_ATTEMPTS, ProfileRepository
from .resolution import ProfileResolver
from .rules import has_unsupported_rules, matches_window_rules, validate_window_rules
from .types import ProfileInfo, ResolvedProfiles, TomlDict

if TYPE_CHECKING:
    from keymasq.session.analog_controls import AnalogControlManager
    from keymasq.session.motion_controls import MotionControlManager
    from keymasq.session.superkeys import SuperkeyManager

log = logging.getLogger("keymasq-session.profiles")

DEFAULT_PROFILE_NAME = "Default"


class ProfileManager:
    """Coordinate profile state snapshots, persistence, and runtime resolution."""

    @staticmethod
    def _with_profile_file_lock[**P, R](
        method: Callable[Concatenate["ProfileManager", P], R],
    ) -> Callable[Concatenate["ProfileManager", P], R]:
        @wraps(method)
        def wrapper(self: "ProfileManager", *args: P.args, **kwargs: P.kwargs) -> R:
            with self._profile_file_lock:
                return method(self, *args, **kwargs)

        return wrapper

    @staticmethod
    def _with_profile_state_lock[**P, R](
        method: Callable[Concatenate["ProfileManager", P], R],
    ) -> Callable[Concatenate["ProfileManager", P], R]:
        @wraps(method)
        def wrapper(self: "ProfileManager", *args: P.args, **kwargs: P.kwargs) -> R:
            with self._profile_state_lock:
                return method(self, *args, **kwargs)

        return wrapper

    def __init__(
        self,
        superkey_manager: "SuperkeyManager | None" = None,
        analog_control_manager: "AnalogControlManager | None" = None,
        motion_control_manager: "MotionControlManager | None" = None,
        auto_create_default_if_empty: bool = False,
    ) -> None:
        paths.ensure_config_dirs()
        self._auto_create_default_if_empty = auto_create_default_if_empty
        self._profiles: dict[str, ProfileInfo] = {}
        self._pending_repairs: set[asyncio.Task[None]] = set()
        self._profile_file_lock = threading.RLock()
        self._profile_state_lock = threading.RLock()
        superkey_exists: Callable[[str], bool] | None = (
            None
            if superkey_manager is None
            else lambda name: bool(superkey_manager.get_superkey(name))
        )
        analog_control_exists: Callable[[str], bool] | None = (
            None
            if analog_control_manager is None
            else lambda name: analog_control_manager.get_analog_control(name) is not None
        )
        motion_control_exists: Callable[[str], bool] | None = (
            None
            if motion_control_manager is None
            else lambda name: motion_control_manager.get_motion_control(name) is not None
        )
        self._codec = ProfileCodec(
            superkey_exists=superkey_exists,
            analog_control_exists=analog_control_exists,
            motion_control_exists=motion_control_exists,
        )
        self._repository = ProfileRepository(self._codec)
        self._load_all()

    def _load_profiles(self, *, strict: bool = False) -> dict[str, ProfileInfo]:
        return self._repository.load_all(
            strict=strict,
            on_created_at_repair=self._schedule_created_at_repair,
        )

    def _load_all(self, *, strict: bool = False) -> None:
        loaded_profiles = self._load_profiles(strict=strict)
        loaded_profiles = self._profiles_with_default_if_empty(
            loaded_profiles,
            strict=strict,
        )
        with self._profile_state_lock:
            self._profiles = loaded_profiles

    @_with_profile_file_lock
    def reload(self) -> None:
        self._load_all(strict=True)

    @contextmanager
    def profile_file_transaction(self) -> Generator[None]:
        with self._profile_file_lock:
            yield

    def _profiles_with_default_if_empty(
        self,
        profiles: dict[str, ProfileInfo],
        *,
        strict: bool = False,
    ) -> dict[str, ProfileInfo]:
        if not self._auto_create_default_if_empty or profiles:
            return profiles

        config = ProfileConfig(
            name=DEFAULT_PROFILE_NAME,
            enabled=True,
            is_permanent=True,
            priority=0,
            notify_on_activation=False,
            created_at=datetime.now(),
        )
        for _attempt in range(MAX_PATH_ATTEMPTS):
            path = self._profile_path_for_name(config.name)
            if path != self._repository.canonical_path(config.name):
                concurrently_loaded = self._load_profiles(strict=strict)
                if concurrently_loaded:
                    return concurrently_loaded
                path = self._profile_path_for_name(config.name)
            try:
                self._write_profile_file(
                    config,
                    path,
                    validate_window_rules=False,
                    exclusive=True,
                )
            except FileExistsError:
                concurrently_loaded = self._load_profiles(strict=strict)
                if concurrently_loaded:
                    return concurrently_loaded
                continue
            break
        else:
            raise RuntimeError(
                f"Unable to allocate default profile storage path for '{config.name}'"
            )

        profiles[config.name] = ProfileInfo(path=path, config=config)
        log.info("Created default profile: %s", path)
        return profiles

    def _schedule_created_at_repair(
        self,
        config: ProfileConfig,
        path: Path,
        reason: str,
    ) -> None:
        created_at = config.created_at or datetime.now()
        log.warning(
            "Profile %s has %s; repairing created_at with current time",
            path,
            reason,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._repair_created_at_if_needed(created_at, path)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                log.error("Failed to repair created_at for %s: %s", path, exc)
            except Exception:
                log.exception("Unexpected failure repairing created_at for %s", path)
            return

        task = loop.create_task(self._repair_created_at_async(created_at, path))
        self._pending_repairs.add(task)
        task.add_done_callback(self._pending_repairs.discard)

    async def _repair_created_at_async(self, created_at: datetime, path: Path) -> None:
        try:
            await asyncio.to_thread(self._repair_created_at_if_needed, created_at, path)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.error("Failed to repair created_at for %s: %s", path, exc)
        except Exception:
            log.exception("Unexpected failure repairing created_at for %s", path)

    def _repair_created_at_if_needed(self, created_at: datetime, path: Path) -> None:
        with self._profile_file_lock:
            repair_created_at(path, created_at)

    @_with_profile_state_lock
    def list_profiles(self) -> list[ProfileInfo]:
        return list(self._profiles.values())

    @_with_profile_state_lock
    def snapshot_profiles(self) -> dict[str, ProfileInfo]:
        return self._profiles.copy()

    @_with_profile_file_lock
    @_with_profile_state_lock
    def snapshot_profiles_for_reload(self) -> dict[str, ProfileInfo]:
        return self._profiles.copy()

    @_with_profile_file_lock
    @_with_profile_state_lock
    def restore_profiles(self, profiles: dict[str, ProfileInfo]) -> None:
        self._profiles = profiles.copy()

    @_with_profile_state_lock
    def get_profile(self, profile_name: str) -> ProfileInfo | None:
        return self._profiles.get(profile_name)

    @_with_profile_state_lock
    def get_next_priority(self) -> int:
        if not self._profiles:
            return 0
        return max(info.config.priority for info in self._profiles.values()) + 1

    def _occupied_profile_paths(self, current_path: Path | None = None) -> set[Path]:
        return {
            info.path
            for info in self._profiles.values()
            if current_path is None or info.path != current_path
        }

    def _profile_path_for_name(
        self,
        profile_name: str,
        current_path: Path | None = None,
        occupied_paths: set[Path] | None = None,
    ) -> Path:
        if occupied_paths is None:
            occupied_paths = self._occupied_profile_paths(current_path)
        return self._repository.allocate_path(
            profile_name,
            current_path=current_path,
            occupied_paths=occupied_paths,
        )

    @_with_profile_file_lock
    def set_profile_enabled(
        self,
        profile_name: str,
        enabled: bool | None,
    ) -> ProfileConfig | None:
        with self._profile_state_lock:
            profile = self._profiles.get(profile_name)
            if profile is None:
                return None
            target_enabled = (not profile.config.enabled) if enabled is None else bool(enabled)
            if profile.config.enabled == target_enabled:
                return profile.config
            updated_config = copy.deepcopy(profile.config)
            updated_config.enabled = target_enabled
            profile_path = profile.path

        self.save_profile(updated_config, path=profile_path)
        return updated_config

    @staticmethod
    def has_unsupported_rules(config: ProfileConfig, capabilities: list[str]) -> bool:
        return has_unsupported_rules(config, capabilities)

    @staticmethod
    def validate_window_rules(window_rules: list[WindowRule]) -> None:
        validate_window_rules(window_rules)

    @staticmethod
    def _matches_window_rules(
        profile: ProfileConfig,
        window_info: TomlDict | None,
    ) -> bool:
        return matches_window_rules(profile, window_info)

    @_with_profile_state_lock
    def resolve_active_profiles(
        self,
        window_info: TomlDict | None = None,
        capabilities: list[str] | None = None,
        hardware_ids: list[str] | None = None,
        runtime_profile_names: list[str] | None = None,
    ) -> ResolvedProfiles:
        return ProfileResolver(self._profiles).resolve(
            window_info=window_info,
            capabilities=capabilities,
            hardware_ids=hardware_ids,
            runtime_profile_names=runtime_profile_names,
        )

    @_with_profile_file_lock
    def save_profile(self, config: ProfileConfig, path: Path | None = None) -> None:
        paths.ensure_config_dirs()
        validate_window_rules(config.window_rules)
        if config.created_at is None:
            config.created_at = datetime.now()

        profile_name = config.name
        current_path = path
        occupied_paths: set[Path] | None = None
        with self._profile_state_lock:
            if current_path is not None:
                path_owner = next(
                    (name for name, info in self._profiles.items() if info.path == current_path),
                    None,
                )
                if path_owner is not None and path_owner != profile_name:
                    raise ValueError(f"Profile storage path is already used by '{path_owner}'")

            existing_profile = self._profiles.get(profile_name)
            if existing_profile is not None:
                if current_path is None:
                    if existing_profile.config is not config:
                        raise ValueError(f"Profile '{profile_name}' already exists")
                    path = existing_profile.path
                elif existing_profile.path != current_path:
                    raise ValueError(f"Profile '{profile_name}' already exists")
                else:
                    path = existing_profile.path
            else:
                occupied_paths = self._occupied_profile_paths(current_path)

        if path is None:
            path = self._profile_path_for_name(
                profile_name,
                current_path=current_path,
                occupied_paths=occupied_paths,
            )
        self._write_profile_file(config, path, validate_window_rules=False)
        with self._profile_state_lock:
            self._profiles[config.name] = ProfileInfo(path=path, config=config)
        log.info("Saved profile: %s", path)

    def _write_profile_file(
        self,
        config: ProfileConfig,
        path: Path,
        validate_window_rules: bool = True,
        exclusive: bool = False,
    ) -> None:
        if validate_window_rules:
            self.validate_window_rules(config.window_rules)
        with self._profile_file_lock:
            self._repository.write(config, path, exclusive=exclusive)

    @_with_profile_file_lock
    def delete_profile(self, name: str) -> bool:
        with self._profile_state_lock:
            profile = self._profiles.get(name)
        if profile is None:
            return False
        if profile.path.exists():
            self._repository.trash(profile.path)
        with self._profile_state_lock:
            self._profiles.pop(name, None)
        log.info("Deleted profile: %s", name)
        return True

    @_with_profile_file_lock
    def rename_profile(self, old_name: str, new_name: str) -> ProfileInfo:
        with self._profile_state_lock:
            if new_name in self._profiles and new_name != old_name:
                raise ValueError(f"Profile '{new_name}' already exists")
            profile = self._profiles.get(old_name)
            if profile is None:
                raise ValueError(f"Profile '{old_name}' not found")
            old_path = profile.path
            renamed_config = copy.deepcopy(profile.config)
            renamed_config.name = new_name
            occupied_paths = self._occupied_profile_paths(old_path)

        new_path = self._profile_path_for_name(
            new_name,
            current_path=old_path,
            occupied_paths=occupied_paths,
        )
        validate_window_rules(renamed_config.window_rules)
        if renamed_config.created_at is None:
            renamed_config.created_at = datetime.now()
        self._write_profile_file(renamed_config, new_path, validate_window_rules=False)

        renamed_profile = ProfileInfo(path=new_path, config=renamed_config)
        with self._profile_state_lock:
            self._profiles.pop(old_name, None)
            self._profiles[new_name] = renamed_profile
        if old_path != new_path and old_path.exists():
            try:
                old_path.unlink()
            except OSError:
                pass
        log.info("Renamed profile: %s -> %s", old_name, new_name)
        return renamed_profile

    @_with_profile_state_lock
    def find_profiles_using_superkey(self, superkey_name: str) -> list[tuple[str, str]]:
        return references.find_superkey(self._profiles.values(), superkey_name)

    @_with_profile_file_lock
    def replace_analog_control_with_suppress(self, analog_control_name: str) -> int:
        count = self._apply_rewrite(
            lambda config: references.remove_analog_control(config, analog_control_name)
        )
        if count:
            log.info(
                "Replaced analog control '%s' with suppress in %d references",
                analog_control_name,
                count,
            )
        return count

    @_with_profile_file_lock
    def rename_analog_control_references(self, old_name: str, new_name: str) -> int:
        count = self._apply_rewrite(
            lambda config: references.rename_analog_control(config, old_name, new_name)
        )
        if count:
            log.info(
                "Renamed analog control references '%s' -> '%s' in %d mappings",
                old_name,
                new_name,
                count,
            )
        return count

    @_with_profile_file_lock
    def replace_motion_control_with_suppress(self, motion_control_name: str) -> int:
        return self._apply_rewrite(
            lambda config: references.remove_motion_control(config, motion_control_name)
        )

    @_with_profile_file_lock
    def rename_motion_control_references(self, old_name: str, new_name: str) -> int:
        return self._apply_rewrite(
            lambda config: references.rename_motion_control(config, old_name, new_name)
        )

    @_with_profile_file_lock
    def rename_superkey_references(self, old_name: str, new_name: str) -> int:
        count = self._apply_rewrite(
            lambda config: references.rename_superkey(config, old_name, new_name)
        )
        if count:
            log.info(
                "Renamed superkey references '%s' -> '%s' in %d profile references",
                old_name,
                new_name,
                count,
            )
        return count

    @_with_profile_file_lock
    def replace_superkey_with_suppress(self, superkey_name: str) -> int:
        count = self._apply_rewrite(
            lambda config: references.remove_superkey(config, superkey_name)
        )
        if count:
            log.info(
                "Replaced superkey '%s' with suppress in %d references",
                superkey_name,
                count,
            )
        return count

    @_with_profile_file_lock
    def remove_device_layers(self, hardware_id: str) -> int:
        return self._apply_rewrite(
            lambda config: references.remove_device_layer(config, hardware_id)
        )

    @_with_profile_file_lock
    def remove_device_button_mappings(self, hardware_id: str, button_id: str) -> int:
        return self._apply_rewrite(
            lambda config: references.remove_button_mapping(
                config,
                hardware_id,
                button_id,
            )
        )

    def _apply_rewrite(
        self,
        rewrite: Callable[[ProfileConfig], references.Rewrite],
    ) -> int:
        count = 0
        for info in self.list_profiles():
            result = rewrite(info.config)
            if result.config is not None:
                self.save_profile(result.config, path=info.path)
            count += result.count
        return count
