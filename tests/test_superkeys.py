from types import SimpleNamespace

import pytest

from keyforge.common import paths
from keyforge.common.models import (
    ActionType,
    MappingAction,
    SuperkeyAction,
    SuperkeyConfig,
    SuperkeyMode,
)
from keyforge.keyforged.runtime.actions import parse_superkey_config
from keyforge.session.manager.payloads import serialize_superkey
from keyforge.session.superkeys import SuperkeyManager


def _parse_manager() -> object:
    return SimpleNamespace(
        _json_object=lambda value: value if isinstance(value, dict) else None,
        _optional_str=lambda value: None if value is None else str(value),
        _int_or_none=lambda value: None if value is None else int(value),
        _float_value=lambda value, default: default if value is None else float(value),
    )


def test_superkey_manager_requires_explicit_mode(
    temp_config_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superkeys_dir = temp_config_dir / "superkeys"
    superkeys_dir.mkdir()
    monkeypatch.setattr(paths, "SUPERKEYS_DIR", superkeys_dir)

    (superkeys_dir / "invalid.toml").write_text(
        """
name = "invalid"

[actions]
tap = [{ action = "keyboard", target = "key_a" }]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    manager = SuperkeyManager()
    config = manager.get_superkey("invalid")

    assert config is None


def test_superkey_manager_rejects_single_table_pattern_slots(
    temp_config_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superkeys_dir = temp_config_dir / "superkeys"
    superkeys_dir.mkdir()
    monkeypatch.setattr(paths, "SUPERKEYS_DIR", superkeys_dir)

    (superkeys_dir / "invalid.toml").write_text(
        """
name = "invalid"
mode = "pattern"

[actions.tap]
action = "keyboard"
target = "key_a"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    manager = SuperkeyManager()
    config = manager.get_superkey("invalid")

    assert config is None


def test_superkey_manager_round_trips_pattern_bundles(temp_config_dir, monkeypatch) -> None:
    superkeys_dir = temp_config_dir / "superkeys"
    superkeys_dir.mkdir()
    monkeypatch.setattr(paths, "SUPERKEYS_DIR", superkeys_dir)

    manager = SuperkeyManager()
    config = SuperkeyConfig(
        name="bundle",
        mode=SuperkeyMode.PATTERN,
        tap_actions=[
            SuperkeyAction(action_type=ActionType.KEYBOARD, target="key_leftctrl"),
            SuperkeyAction(action_type=ActionType.KEYBOARD, target="key_c"),
        ],
        hold_actions=[
            SuperkeyAction(action_type=ActionType.MACRO, macro_name="demo"),
        ],
    )

    manager.save_superkey(config)
    reloaded = SuperkeyManager().get_superkey("bundle")

    assert reloaded is not None
    assert reloaded.mode == SuperkeyMode.PATTERN
    assert [action.target for action in reloaded.tap_actions] == ["key_leftctrl", "key_c"]
    assert [action.macro_name for action in reloaded.hold_actions] == ["demo"]
    text = (superkeys_dir / "bundle.toml").read_text(encoding="utf-8")
    assert 'mode = "pattern"' in text
    assert "tap = [" in text


def test_superkey_manager_round_trips_overload_actions(temp_config_dir, monkeypatch) -> None:
    superkeys_dir = temp_config_dir / "superkeys"
    superkeys_dir.mkdir()
    monkeypatch.setattr(paths, "SUPERKEYS_DIR", superkeys_dir)

    manager = SuperkeyManager()
    config = SuperkeyConfig(
        name="overload",
        mode=SuperkeyMode.OVERLOAD,
        overload_actions=[
            MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
            MappingAction(action_type=ActionType.PROFILE_TOGGLE, profile_name="Gaming"),
            MappingAction(action_type=ActionType.EXEC, cmd="notify-send overload"),
        ],
    )

    manager.save_superkey(config)
    reloaded = SuperkeyManager().get_superkey("overload")

    assert reloaded is not None
    assert reloaded.mode == SuperkeyMode.OVERLOAD
    assert [action.action_type for action in reloaded.overload_actions] == [
        ActionType.KEYBOARD,
        ActionType.PROFILE_TOGGLE,
        ActionType.EXEC,
    ]
    assert reloaded.overload_actions[1].profile_name == "Gaming"
    assert reloaded.overload_actions[2].cmd == "notify-send overload"


def test_superkey_runtime_payload_round_trips_overload_actions() -> None:
    manager = SimpleNamespace(
        exec_state=SimpleNamespace(next_superkey_exec_ref=10000, superkey_exec_refs={}),
        superkeys=SimpleNamespace(get_superkey=lambda _name: None),
    )
    config = SuperkeyConfig(
        name="runtime_overload",
        mode=SuperkeyMode.OVERLOAD,
        overload_actions=[
            MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
            MappingAction(action_type=ActionType.EXEC, cmd="echo demo"),
        ],
    )

    payload = serialize_superkey(manager, config, "1234:5678")
    parsed = parse_superkey_config(
        _parse_manager(),
        payload,
        json_object=lambda value: value if isinstance(value, dict) else None,
        str_value=lambda value, default="": default if value is None else str(value),
        int_value=lambda value, default=0: default if value is None else int(value),
        parse_superkey_action=lambda *_args, **_kwargs: None,
    )

    assert parsed.mode == SuperkeyMode.OVERLOAD
    assert [action.action_type for action in parsed.overload_actions] == [
        ActionType.KEYBOARD,
        ActionType.EXEC,
    ]
    assert parsed.overload_actions[1].exec_ref == 10000
    assert manager.exec_state.superkey_exec_refs[10000] == ("1234:5678", "echo demo")


def test_superkey_runtime_payload_requires_explicit_mode() -> None:
    with pytest.raises(TypeError, match="include a mode"):
        parse_superkey_config(
            _parse_manager(),
            {"name": "missing_mode"},
            json_object=lambda value: value if isinstance(value, dict) else None,
            str_value=lambda value, default="": default if value is None else str(value),
            int_value=lambda value, default=0: default if value is None else int(value),
            parse_superkey_action=lambda *_args, **_kwargs: None,
        )


def test_superkey_runtime_payload_requires_bundle_lists() -> None:
    with pytest.raises(TypeError, match="must be a list"):
        parse_superkey_config(
            _parse_manager(),
            {
                "name": "bad_bundle",
                "mode": "pattern",
                "tap_actions": {"action": "keyboard", "target": "key_a"},
            },
            json_object=lambda value: value if isinstance(value, dict) else None,
            str_value=lambda value, default="": default if value is None else str(value),
            int_value=lambda value, default=0: default if value is None else int(value),
            parse_superkey_action=lambda *_args, **_kwargs: None,
        )


def test_superkey_manager_rejects_nested_overload_superkeys(
    temp_config_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superkeys_dir = temp_config_dir / "superkeys"
    superkeys_dir.mkdir()
    monkeypatch.setattr(paths, "SUPERKEYS_DIR", superkeys_dir)

    manager = SuperkeyManager()
    config = SuperkeyConfig(
        name="bad_overload",
        mode=SuperkeyMode.OVERLOAD,
        overload_actions=[
            MappingAction(action_type=ActionType.SUPERKEY, superkey_name="other"),
        ],
    )

    with pytest.raises(ValueError):
        manager.save_superkey(config)
