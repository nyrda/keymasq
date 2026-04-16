from types import SimpleNamespace

import pytest

from keymasq.common import paths
from keymasq.common.models import (
    ActionType,
    ComboConfig,
    ComboEvent,
    ComboStep,
    MappingAction,
    ProfileConfig,
    SuperkeyAction,
    SuperkeyConfig,
    SuperkeyMode,
    mapping_action_to_superkey_action,
    superkey_action_to_mapping_action,
)
from keymasq.keymasqd.runtime.actions import parse_superkey_config
from keymasq.session.manager.payloads import (
    clear_combo_exec_refs,
    combo_action_signature_payload,
    combo_action_to_payload,
    serialize_superkey,
)
from keymasq.session.profiles import ProfileManager
from keymasq.session.superkeys import SuperkeyManager


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


def test_superkey_action_roundtrip_preserves_shared_fields() -> None:
    action = MappingAction(
        action_type=ActionType.KEYBOARD,
        target="key_a",
        rapidfire_enabled=True,
        rapidfire_hold_ms=40,
        rapidfire_wait_ms=60,
    )

    superkey_action = mapping_action_to_superkey_action(action)
    round_tripped = superkey_action_to_mapping_action(superkey_action)

    assert round_tripped.action_type == ActionType.KEYBOARD
    assert round_tripped.target == "key_a"
    assert round_tripped.rapidfire_enabled is True
    assert round_tripped.rapidfire_hold_ms == 40
    assert round_tripped.rapidfire_wait_ms == 60


def test_superkey_action_roundtrip_strips_unsupported_rapidfire() -> None:
    action = MappingAction(
        action_type=ActionType.MACRO,
        macro_name="demo",
        rapidfire_enabled=True,
        rapidfire_hold_ms=40,
        rapidfire_wait_ms=60,
    )

    superkey_action = mapping_action_to_superkey_action(action)
    round_tripped = superkey_action_to_mapping_action(superkey_action)

    assert superkey_action.rapidfire_enabled is False
    assert round_tripped.rapidfire_enabled is False
    assert round_tripped.rapidfire_hold_ms == 20
    assert round_tripped.rapidfire_wait_ms == 20


def test_superkey_manager_round_trips_extended_pattern_actions(
    temp_config_dir,
    monkeypatch,
) -> None:
    superkeys_dir = temp_config_dir / "superkeys"
    superkeys_dir.mkdir()
    monkeypatch.setattr(paths, "SUPERKEYS_DIR", superkeys_dir)

    manager = SuperkeyManager()
    config = SuperkeyConfig(
        name="extended-pattern",
        mode=SuperkeyMode.PATTERN,
        tap_actions=[
            SuperkeyAction(action_type=ActionType.PROFILE_TOGGLE, profile_name="Gaming"),
        ],
        double_tap_actions=[
            SuperkeyAction(
                action_type=ActionType.COMPOSITOR_DISPATCH,
                compositor_id="hyprland",
                compositor_dispatcher="workspace",
                compositor_args="e+1",
            ),
        ],
        hold_actions=[
            SuperkeyAction(action_type=ActionType.MOUSE_MOVE_REL, move_x=12, move_y=-4),
        ],
        tap_hold_actions=[
            SuperkeyAction(action_type=ActionType.CANCEL_MACRO_PLAYBACK),
        ],
    )

    manager.save_superkey(config)
    reloaded = SuperkeyManager().get_superkey("extended-pattern")

    assert reloaded is not None
    assert reloaded.tap_actions[0].action_type == ActionType.PROFILE_TOGGLE
    assert reloaded.tap_actions[0].profile_name == "Gaming"
    assert reloaded.double_tap_actions[0].action_type == ActionType.COMPOSITOR_DISPATCH
    assert reloaded.double_tap_actions[0].compositor_dispatcher == "workspace"
    assert reloaded.double_tap_actions[0].compositor_args == "e+1"
    assert reloaded.hold_actions[0].action_type == ActionType.MOUSE_MOVE_REL
    assert reloaded.hold_actions[0].move_x == 12
    assert reloaded.hold_actions[0].move_y == -4
    assert reloaded.tap_hold_actions[0].action_type == ActionType.CANCEL_MACRO_PLAYBACK


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


def test_superkey_manager_warns_and_strips_manual_unsupported_rapidfire(
    temp_config_dir,
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    superkeys_dir = temp_config_dir / "superkeys"
    superkeys_dir.mkdir()
    monkeypatch.setattr(paths, "SUPERKEYS_DIR", superkeys_dir)
    (superkeys_dir / "warn.toml").write_text(
        """
name = "warn"
mode = "pattern"

[[actions.hold]]
action = "macro"
target = "demo"
rapidfire_enabled = true
rapidfire_hold_ms = 40
rapidfire_wait_ms = 60
""".strip(),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING", logger="keymasq-session.superkeys"):
        config = SuperkeyManager().get_superkey("warn")

    assert config is not None
    assert config.hold_actions[0].rapidfire_enabled is False
    assert "Ignoring rapidfire for unsupported macro action in superkey config" in caplog.text


def test_superkey_runtime_payload_round_trips_overload_actions() -> None:
    manager = SimpleNamespace(
        exec_state=SimpleNamespace(
            next_superkey_exec_ref=10000,
            superkey_exec_refs={},
            combo_superkey_exec_refs=set(),
        ),
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
        optional_str=lambda value: None if value is None else str(value),
        int_value=lambda value, default=0: default if value is None else int(value),
        int_or_none=lambda value: None if value is None else int(value),
        float_value=lambda value, default=0.0: default if value is None else float(value),
        parse_superkey_action=lambda *_args, **_kwargs: None,
    )

    assert parsed.mode == SuperkeyMode.OVERLOAD
    assert [action.action_type for action in parsed.overload_actions] == [
        ActionType.KEYBOARD,
        ActionType.EXEC,
    ]
    assert parsed.overload_actions[1].exec_ref == 10000
    assert manager.exec_state.superkey_exec_refs[10000] == ("1234:5678", "echo demo")


def test_combo_superkey_payload_tracks_combo_scoped_exec_refs() -> None:
    config = SuperkeyConfig(
        name="combo_exec",
        mode=SuperkeyMode.PATTERN,
        tap_actions=[SuperkeyAction(action_type=ActionType.EXEC, cmd="echo combo")],
    )
    manager = SimpleNamespace(
        exec_state=SimpleNamespace(
            next_superkey_exec_ref=10000,
            superkey_exec_refs={},
            combo_superkey_exec_refs=set(),
        ),
        superkeys=SimpleNamespace(
            get_superkey=lambda name: config if name == "combo_exec" else None
        ),
    )

    payload = combo_action_to_payload(
        manager,
        MappingAction(action_type=ActionType.SUPERKEY, superkey_name="combo_exec"),
        step_count=1,
    )

    assert payload is not None
    assert payload["action"] == "superkey"
    superkey_payload = payload["superkey"]
    assert isinstance(superkey_payload, dict)
    tap_actions = superkey_payload["tap_actions"]
    assert isinstance(tap_actions, list)
    assert tap_actions[0]["exec_ref"] == 10000
    assert manager.exec_state.combo_superkey_exec_refs == {10000}
    assert manager.exec_state.superkey_exec_refs[10000] == ("combo", "echo combo")


def test_combo_superkey_multistep_payload_and_signature_strip_double_tap_slots() -> None:
    config = SuperkeyConfig(
        name="pattern_combo",
        mode=SuperkeyMode.PATTERN,
        tap_actions=[SuperkeyAction(action_type=ActionType.KEYBOARD, target="key_a")],
        double_tap_actions=[SuperkeyAction(action_type=ActionType.KEYBOARD, target="key_b")],
        hold_actions=[SuperkeyAction(action_type=ActionType.KEYBOARD, target="key_c")],
        tap_hold_actions=[SuperkeyAction(action_type=ActionType.KEYBOARD, target="key_d")],
    )
    manager = SimpleNamespace(
        exec_state=SimpleNamespace(
            next_superkey_exec_ref=10000,
            superkey_exec_refs={},
            combo_superkey_exec_refs=set(),
        ),
        superkeys=SimpleNamespace(
            get_superkey=lambda name: config if name == "pattern_combo" else None
        ),
    )
    action = MappingAction(action_type=ActionType.SUPERKEY, superkey_name="pattern_combo")

    payload = combo_action_to_payload(manager, action, step_count=2)
    signature = combo_action_signature_payload(manager, action, step_count=2)

    assert payload is not None
    assert signature is not None
    payload_superkey = payload["superkey"]
    signature_superkey = signature["superkey"]
    assert isinstance(payload_superkey, dict)
    assert isinstance(signature_superkey, dict)
    assert "double_tap_actions" not in payload_superkey
    assert "tap_hold_actions" not in payload_superkey
    assert "double_tap_actions" not in signature_superkey
    assert "tap_hold_actions" not in signature_superkey
    assert "tap_actions" in payload_superkey
    assert "hold_actions" in payload_superkey


def test_clear_combo_exec_refs_clears_combo_superkey_exec_refs() -> None:
    manager = SimpleNamespace(
        exec_state=SimpleNamespace(
            combo_exec_refs={7},
            combo_superkey_exec_refs={10000},
            exec_refs={7: "echo combo"},
            superkey_exec_refs={10000: ("combo", "echo super")},
        )
    )

    clear_combo_exec_refs(manager)

    assert manager.exec_state.combo_exec_refs == set()
    assert manager.exec_state.combo_superkey_exec_refs == set()
    assert manager.exec_state.exec_refs == {}
    assert manager.exec_state.superkey_exec_refs == {}


def test_superkey_runtime_payload_requires_explicit_mode() -> None:
    with pytest.raises(TypeError, match="include a mode"):
        parse_superkey_config(
            _parse_manager(),
            {"name": "missing_mode"},
            json_object=lambda value: value if isinstance(value, dict) else None,
            str_value=lambda value, default="": default if value is None else str(value),
            optional_str=lambda value: None if value is None else str(value),
            int_value=lambda value, default=0: default if value is None else int(value),
            int_or_none=lambda value: None if value is None else int(value),
            float_value=lambda value, default=0.0: default if value is None else float(value),
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
            optional_str=lambda value: None if value is None else str(value),
            int_value=lambda value, default=0: default if value is None else int(value),
            int_or_none=lambda value: None if value is None else int(value),
            float_value=lambda value, default=0.0: default if value is None else float(value),
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

    with pytest.raises(ValueError, match="nested superkeys are not allowed"):
        manager.save_superkey(config)


def test_profile_manager_finds_and_replaces_combo_superkey_references(
    temp_config_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles_dir = temp_config_dir / "profiles"
    profiles_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(paths, "PROFILES_DIR", profiles_dir)

    manager = ProfileManager()
    profile = ProfileConfig(
        name="Desktop",
        combos=[
            ComboConfig(
                id="combo-1",
                steps=[
                    ComboStep(events=[ComboEvent(evdev="key_a", hardware_id="1234:5678")]),
                ],
                action=MappingAction(
                    action_type=ActionType.SUPERKEY,
                    superkey_name="combo-superkey",
                ),
            )
        ],
    )
    manager.save_profile(profile)

    assert manager.find_profiles_using_superkey("combo-superkey") == [("combo", "Desktop")]

    replaced = manager.replace_superkey_with_suppress("combo-superkey")
    updated = manager.get_profile("Desktop")

    assert replaced == 1
    assert updated is not None
    assert updated.config.combos[0].action is not None
    assert updated.config.combos[0].action.action_type == ActionType.SUPPRESS
