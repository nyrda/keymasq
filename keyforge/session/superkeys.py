import logging
import tomllib
from pathlib import Path

import tomli_w

from keyforge.common import paths
from keyforge.common.models import (
    ActionType,
    SuperkeyAction,
    SuperkeyConfig,
)

log = logging.getLogger("keyforge-session.superkeys")


class SuperkeyManager:
    def __init__(self) -> None:
        paths.ensure_config_dirs()
        self._superkeys: dict[str, SuperkeyConfig] = {}
        self._load_all()

    def _load_all(self) -> None:
        self._superkeys.clear()

        if not paths.SUPERKEYS_DIR.exists():
            return

        for superkey_file in paths.SUPERKEYS_DIR.glob("*.toml"):
            try:
                config = self._load_superkey(superkey_file)
                if config:
                    self._superkeys[config.name] = config
            except Exception as e:
                log.error(f"Failed to load superkey {superkey_file}: {e}")

    def _load_superkey(self, path: Path) -> SuperkeyConfig | None:
        with open(path, "rb") as f:
            data = tomllib.load(f)

        name = data.get("name", path.stem)

        timing = data.get("timing", {})

        actions_data = data.get("actions", {})

        return SuperkeyConfig(
            name=name,
            description=data.get("description"),
            tap_action=self._parse_superkey_action(actions_data.get("tap")),
            double_tap_action=self._parse_superkey_action(actions_data.get("double_tap")),
            hold_action=self._parse_superkey_action(actions_data.get("hold")),
            tap_hold_action=self._parse_superkey_action(actions_data.get("tap_hold")),
            tap_timeout_ms=timing.get("tap_timeout_ms", 200),
            double_tap_window_ms=timing.get("double_tap_window_ms", 300),
            hold_threshold_ms=timing.get("hold_threshold_ms", 300),
        )

    def _parse_superkey_action(self, data: dict | None) -> SuperkeyAction | None:
        if not data:
            return None

        action_type_str = data.get("action", "passthrough")

        try:
            action_type = ActionType(action_type_str)
        except ValueError:
            log.warning(f"Unknown action type '{action_type_str}'")
            return None

        if action_type not in (
            ActionType.KEYBOARD,
            ActionType.MOUSE,
            ActionType.GAMEPAD,
            ActionType.EXEC,
            ActionType.MACRO,
        ):
            log.warning(f"Invalid superkey action type '{action_type_str}'")
            return None

        return SuperkeyAction(
            action_type=action_type,
            target=data.get("target"),
            cmd=data.get("cmd"),
            macro_name=(
                data.get("macro_name")
                or (data.get("target") if action_type == ActionType.MACRO else None)
            ),
            rapidfire_enabled=data.get("rapidfire_enabled", False),
            rapidfire_hold_ms=data.get("rapidfire_hold_ms", 20),
            rapidfire_wait_ms=data.get("rapidfire_wait_ms", 20),
        )

    def get_superkey(self, name: str) -> SuperkeyConfig | None:
        return self._superkeys.get(name)

    def list_superkeys(self) -> list[str]:
        return sorted(self._superkeys.keys())

    def get_all_superkeys(self) -> dict[str, SuperkeyConfig]:
        return self._superkeys.copy()

    def save_superkey(self, config: SuperkeyConfig) -> None:
        paths.ensure_config_dirs()

        safe_name = self._sanitize_name(config.name)
        path = paths.SUPERKEYS_DIR / f"{safe_name}.toml"

        data = {
            "name": config.name,
        }

        if config.description:
            data["description"] = config.description

        timing = {}
        if config.tap_timeout_ms != 200:
            timing["tap_timeout_ms"] = config.tap_timeout_ms
        if config.double_tap_window_ms != 300:
            timing["double_tap_window_ms"] = config.double_tap_window_ms
        if config.hold_threshold_ms != 300:
            timing["hold_threshold_ms"] = config.hold_threshold_ms
        if timing:
            data["timing"] = timing

        actions = {}
        if config.tap_action:
            actions["tap"] = self._serialize_action(config.tap_action)
        if config.double_tap_action:
            actions["double_tap"] = self._serialize_action(config.double_tap_action)
        if config.hold_action:
            actions["hold"] = self._serialize_action(config.hold_action)
        if config.tap_hold_action:
            actions["tap_hold"] = self._serialize_action(config.tap_hold_action)
        if actions:
            data["actions"] = actions

        with open(path, "wb") as f:
            tomli_w.dump(data, f)

        self._superkeys[config.name] = config
        log.info(f"Saved superkey: {config.name}")

    def _serialize_action(self, action: SuperkeyAction) -> dict:
        data = {"action": action.action_type.value}

        if action.target:
            data["target"] = action.target
        if action.cmd:
            data["cmd"] = action.cmd
        if action.macro_name:
            data["macro_name"] = action.macro_name

        if action.rapidfire_enabled:
            data["rapidfire_enabled"] = True
            data["rapidfire_hold_ms"] = action.rapidfire_hold_ms
            data["rapidfire_wait_ms"] = action.rapidfire_wait_ms

        return data

    def delete_superkey(self, name: str) -> bool:
        if name not in self._superkeys:
            return False

        config = self._superkeys[name]
        safe_name = self._sanitize_name(config.name)
        path = paths.SUPERKEYS_DIR / f"{safe_name}.toml"

        if path.exists():
            path.unlink()

        del self._superkeys[name]
        log.info(f"Deleted superkey: {name}")
        return True

    def rename_superkey(self, old_name: str, new_name: str) -> bool:
        if old_name not in self._superkeys:
            return False

        if new_name in self._superkeys and new_name != old_name:
            log.warning(f"Superkey '{new_name}' already exists")
            return False

        config = self._superkeys[old_name]
        old_path = paths.SUPERKEYS_DIR / f"{self._sanitize_name(old_name)}.toml"

        config.name = new_name
        self.save_superkey(config)

        if old_path.exists():
            old_path.unlink()

        del self._superkeys[old_name]

        log.info(f"Renamed superkey: {old_name} -> {new_name}")
        return True

    def _sanitize_name(self, name: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        return safe.lower()

    def reload(self) -> None:
        self._load_all()
        log.info("Reloaded all superkeys")
