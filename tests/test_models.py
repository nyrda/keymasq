from datetime import datetime, timedelta
from pathlib import Path

import pytest

from keyforge.common.models import (
    ActionType,
    ComboConfig,
    ComboEvent,
    ComboStep,
    DeviceProfileLayer,
    MappingAction,
    ProfileConfig,
    ProfileState,
    WindowRule,
    is_protected_button,
)
from keyforge.session.hardware import HardwareManager
from keyforge.session.profiles import ProfileManager


class TestHardwareManager:
    def test_save_and_load_hardware(self, temp_config_dir, sample_hardware_config):
        manager = HardwareManager()
        manager.save_hardware(sample_hardware_config)

        loaded = manager.get_hardware(sample_hardware_config.hardware_id)

        assert loaded is not None
        assert loaded.name == sample_hardware_config.name
        assert loaded.vendor_id == sample_hardware_config.vendor_id
        assert loaded.product_id == sample_hardware_config.product_id
        assert len(loaded.buttons) == len(sample_hardware_config.buttons)

    def test_list_hardware(self, temp_config_dir, sample_hardware_config):
        manager = HardwareManager()
        manager.save_hardware(sample_hardware_config)

        hardware_list = manager.list_hardware()

        assert len(hardware_list) == 1
        assert hardware_list[0].hardware_id == sample_hardware_config.hardware_id

    def test_get_nonexistent_hardware(self, temp_config_dir):
        manager = HardwareManager()

        result = manager.get_hardware("ffff:ffff")

        assert result is None

    def test_hardware_id_format(self, sample_hardware_config):
        assert sample_hardware_config.hardware_id == "1234:5678"


class TestProfileManager:
    def test_save_and_load_profile(self, temp_config_dir, sample_profile_config):
        manager = ProfileManager()
        manager.save_profile(sample_profile_config)

        profiles = manager.list_profiles()

        assert len(profiles) == 1
        assert profiles[0].config.name == sample_profile_config.name
        assert len(profiles[0].config.device_layers["1234:5678"].mappings) == 2

    def test_multiple_profiles_global(self, temp_config_dir, sample_profile_config):
        manager = ProfileManager()
        manager.save_profile(sample_profile_config)

        profile2 = ProfileConfig(
            name="Test Profile 2",
            enabled=False,
            device_layers={
                "1234:5678": DeviceProfileLayer(
                    hardware_id="1234:5678",
                    mappings={"btn_back": MappingAction(action_type=ActionType.SUPPRESS)},
                )
            },
        )
        manager.save_profile(profile2)

        profiles = manager.list_profiles()

        assert len(profiles) == 2
        names = [p.config.name for p in profiles]
        assert "Test Profile" in names
        assert "Test Profile 2" in names

    def test_save_profile_rejects_duplicate_visible_name(
        self, temp_config_dir, sample_profile_config
    ):
        manager = ProfileManager()
        manager.save_profile(sample_profile_config)

        duplicate = ProfileConfig(
            name=sample_profile_config.name,
            enabled=False,
            device_layers={},
        )

        try:
            manager.save_profile(duplicate)
        except ValueError as exc:
            assert "already exists" in str(exc)
        else:
            raise AssertionError("Expected duplicate profile name to be rejected")

    def test_resolve_active_profiles_merges_layers(self, temp_config_dir):
        manager = ProfileManager()
        base = ProfileConfig(
            name="Base",
            enabled=True,
            is_permanent=True,
            priority=1,
            created_at=datetime.now(),
            device_layers={
                "1234:5678": DeviceProfileLayer(
                    hardware_id="1234:5678",
                    mappings={
                        "btn_back": MappingAction(action_type=ActionType.KEYBOARD, target="key_1")
                    },
                )
            },
        )
        newer = ProfileConfig(
            name="Overlay",
            enabled=True,
            is_permanent=True,
            priority=1,
            created_at=datetime.now() + timedelta(seconds=1),
            device_layers={
                "1234:5678": DeviceProfileLayer(
                    hardware_id="1234:5678",
                    mappings={
                        "btn_back": MappingAction(action_type=ActionType.KEYBOARD, target="key_2")
                    },
                )
            },
        )
        manager.save_profile(base)
        manager.save_profile(newer)

        resolved = manager.resolve_active_profiles(hardware_ids=["1234:5678"])

        assert [profile.name for profile in resolved.active_profiles] == ["Base", "Overlay"]
        assert resolved.devices["1234:5678"].mappings["btn_back"].target == "key_2"

    def test_resolve_active_profiles_collects_combo_sources(self, temp_config_dir):
        manager = ProfileManager()
        profile = ProfileConfig(
            name="Combo Profile",
            enabled=True,
            is_permanent=True,
            combos=[
                ComboConfig(
                    id="combo-1",
                    name="Side + Extra",
                    steps=[
                        ComboStep(
                            events=[
                                ComboEvent(
                                    hardware_id="1234:5678",
                                    source="mouse",
                                    evdev="btn_side",
                                ),
                                ComboEvent(
                                    hardware_id="1234:5678",
                                    source="mouse",
                                    evdev="btn_extra",
                                ),
                            ]
                        )
                    ],
                    action=MappingAction(
                        action_type=ActionType.PROFILE_TOGGLE,
                        profile_name="Gaming",
                    ),
                )
            ],
        )
        manager.save_profile(profile)

        resolved = manager.resolve_active_profiles(hardware_ids=["1234:5678"])

        assert len(resolved.combos) == 1
        assert resolved.devices["1234:5678"].combo_event_count == 2
        assert resolved.devices["1234:5678"].combo_sources == {"mouse"}

    def test_combo_step_timeout_round_trips_in_profile_storage(self, temp_config_dir):
        manager = ProfileManager()
        profile = ProfileConfig(
            name="Combo Timeout",
            enabled=True,
            is_permanent=True,
            combos=[
                ComboConfig(
                    id="combo-1",
                    name="Quick Save",
                    steps=[
                        ComboStep(
                            events=[
                                ComboEvent(
                                    hardware_id="1234:5678",
                                    source="kbd",
                                    evdev="key_leftctrl",
                                ),
                                ComboEvent(
                                    hardware_id="1234:5678",
                                    source="kbd",
                                    evdev="key_s",
                                ),
                            ]
                        ),
                        ComboStep(
                            events=[
                                ComboEvent(
                                    hardware_id="1234:5678",
                                    source="kbd",
                                    evdev="key_1",
                                )
                            ],
                            timeout_ms=750,
                        ),
                    ],
                    action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
                )
            ],
        )
        manager.save_profile(profile)

        loaded = manager.get_profile("Combo Timeout")

        assert loaded is not None
        assert loaded.config.combos[0].steps[0].timeout_ms is None
        assert loaded.config.combos[0].steps[1].timeout_ms == 750

    def test_disabled_profile_not_active(self, temp_config_dir):
        manager = ProfileManager()

        profile = ProfileConfig(
            name="Disabled Profile",
            enabled=False,
            is_permanent=True,
            device_layers={},
        )
        manager.save_profile(profile)

        result = manager.resolve_active_profiles(hardware_ids=["1234:5678"])

        assert result.active_profiles == []

    def test_profile_storage_path_is_sanitized(self, temp_config_dir):
        manager = ProfileManager()
        profile = ProfileConfig(name="../../Work / Gaming", enabled=True, device_layers={})

        manager.save_profile(profile)

        info = manager.get_profile(profile.name)

        assert info is not None
        assert info.path.parent == temp_config_dir / "profiles"
        assert info.path.name == "Work_Gaming.toml"
        assert info.path.exists()
        assert not (temp_config_dir / "Work_Gaming.toml").exists()

    def test_profile_storage_path_collision_gets_suffix(self, temp_config_dir):
        manager = ProfileManager()

        manager.save_profile(ProfileConfig(name="Work/Mode", enabled=True, device_layers={}))
        manager.save_profile(ProfileConfig(name="Work_Mode", enabled=True, device_layers={}))

        first = manager.get_profile("Work/Mode")
        second = manager.get_profile("Work_Mode")

        assert first is not None
        assert second is not None
        assert first.path.name == "Work_Mode.toml"
        assert second.path.name == "Work_Mode_2.toml"

    def test_rename_profile_reuses_existing_sanitized_path(self, temp_config_dir):
        manager = ProfileManager()
        profile = ProfileConfig(name="Work/Mode", enabled=True, device_layers={})

        manager.save_profile(profile)
        original_path = manager.get_profile("Work/Mode").path

        renamed = manager.rename_profile("Work/Mode", "Work?Mode")

        assert renamed is not None
        assert renamed.path == original_path
        assert renamed.path.name == "Work_Mode.toml"
        assert renamed.path.exists()
        assert 'name = "Work?Mode"' in renamed.path.read_text(encoding="utf-8")

        reloaded = ProfileManager()

        assert reloaded.get_profile("Work?Mode") is not None
        assert reloaded.get_profile("Work/Mode") is None

    def test_rename_profile_rejects_duplicate_visible_name(self, temp_config_dir):
        manager = ProfileManager()

        manager.save_profile(ProfileConfig(name="Desktop", enabled=True, device_layers={}))
        manager.save_profile(ProfileConfig(name="Gaming", enabled=True, device_layers={}))

        try:
            manager.rename_profile("Desktop", "Gaming")
        except ValueError as exc:
            assert "already exists" in str(exc)
        else:
            raise AssertionError("Expected duplicate profile rename to be rejected")

    def test_profile_path_allocation_has_attempt_guard(self, temp_config_dir, monkeypatch):
        manager = ProfileManager()

        monkeypatch.setattr(
            "keyforge.session.profiles.MAX_PROFILE_PATH_ATTEMPTS",
            3,
        )
        monkeypatch.setattr(Path, "exists", lambda self: True)

        with pytest.raises(RuntimeError, match="Unable to allocate profile storage path"):
            manager._profile_path_for_name("Desktop")

    def test_save_profile_rejects_invalid_window_rule_regex(self, temp_config_dir):
        manager = ProfileManager()
        profile = ProfileConfig(
            name="Broken",
            enabled=True,
            window_rules=[WindowRule(field="class", pattern="(")],
            device_layers={},
        )

        try:
            manager.save_profile(profile)
        except ValueError as exc:
            assert "Invalid regex" in str(exc)
        else:
            raise AssertionError("Expected ValueError for invalid regex")

    def test_invalid_window_rule_regex_from_disk_does_not_crash_matching(self, temp_config_dir):
        profile_path = Path(temp_config_dir) / "profiles" / "broken.toml"
        profile_path.write_text(
            """
[profile]
name = "Broken"
enabled = true
is_permanent = false
priority = 1
notify_on_activation = true
created_at = "2026-03-09T12:34:56"

[[profile.window_rules]]
field = "class"
pattern = "("
""".strip(),
            encoding="utf-8",
        )

        manager = ProfileManager()
        resolved = manager.resolve_active_profiles(
            window_info={"class": "steam"},
            hardware_ids=["1234:5678"],
        )

        assert resolved.active_profiles == []

    def test_missing_created_at_is_repaired_on_load(self, temp_config_dir):
        profile_path = Path(temp_config_dir) / "profiles" / "missing-created-at.toml"
        profile_path.write_text(
            """
[profile]
name = "Missing Created"
enabled = true
is_permanent = true
priority = 1
notify_on_activation = true
""".strip(),
            encoding="utf-8",
        )

        manager = ProfileManager()
        loaded = manager.get_profile("Missing Created")

        assert loaded is not None
        assert loaded.config.created_at is not None
        assert 'created_at = "' in profile_path.read_text(encoding="utf-8")

    def test_malformed_created_at_is_repaired_on_load(self, temp_config_dir):
        profile_path = Path(temp_config_dir) / "profiles" / "bad-created-at.toml"
        profile_path.write_text(
            """
[profile]
name = "Bad Created"
enabled = true
is_permanent = true
priority = 1
notify_on_activation = true
created_at = "not-a-date"
""".strip(),
            encoding="utf-8",
        )

        manager = ProfileManager()
        loaded = manager.get_profile("Bad Created")

        assert loaded is not None
        assert loaded.config.created_at is not None
        content = profile_path.read_text(encoding="utf-8")
        assert 'created_at = "' in content
        assert 'created_at = "not-a-date"' not in content

    def test_remove_device_button_mappings_clears_matching_profile_entries(self, temp_config_dir):
        manager = ProfileManager()
        profile = ProfileConfig(
            name="Test Profile",
            enabled=True,
            device_layers={
                "1234:5678": DeviceProfileLayer(
                    hardware_id="1234:5678",
                    mappings={
                        "btn_back": MappingAction(action_type=ActionType.KEYBOARD, target="key_1"),
                        "btn_forward": MappingAction(
                            action_type=ActionType.KEYBOARD,
                            target="key_2",
                        ),
                    },
                ),
                "9999:0001": DeviceProfileLayer(
                    hardware_id="9999:0001",
                    mappings={
                        "btn_back": MappingAction(action_type=ActionType.KEYBOARD, target="key_3")
                    },
                ),
            },
        )
        manager.save_profile(profile)

        updated = manager.remove_device_button_mappings("1234:5678", "btn_back")

        assert updated == 1
        reloaded = ProfileManager()
        saved = reloaded.get_profile("Test Profile")
        assert saved is not None
        assert "btn_back" not in saved.config.device_layers["1234:5678"].mappings
        assert "btn_forward" in saved.config.device_layers["1234:5678"].mappings
        assert "btn_back" in saved.config.device_layers["9999:0001"].mappings


class TestMappingAction:
    def test_keyboard_action(self):
        action = MappingAction(action_type=ActionType.KEYBOARD, target="key_a")

        assert action.action_type == ActionType.KEYBOARD
        assert action.target == "key_a"

    def test_keyboard_with_rapidfire(self):
        action = MappingAction(
            action_type=ActionType.KEYBOARD,
            target="key_a",
            rapidfire_enabled=True,
            rapidfire_hold_ms=50,
            rapidfire_wait_ms=30,
        )

        assert action.action_type == ActionType.KEYBOARD
        assert action.rapidfire_enabled is True
        assert action.rapidfire_hold_ms == 50
        assert action.rapidfire_wait_ms == 30

    def test_mouse_action(self):
        action = MappingAction(action_type=ActionType.MOUSE, target="btn_left")

        assert action.action_type == ActionType.MOUSE
        assert action.target == "btn_left"

    def test_gamepad_action(self):
        action = MappingAction(action_type=ActionType.GAMEPAD, target="btn_south")

        assert action.action_type == ActionType.GAMEPAD
        assert action.target == "btn_south"

    def test_exec_action(self):
        action = MappingAction(
            action_type=ActionType.EXEC,
            cmd="playerctl play-pause",
        )

        assert action.action_type == ActionType.EXEC
        assert action.cmd == "playerctl play-pause"

    def test_suppress_action(self):
        action = MappingAction(action_type=ActionType.SUPPRESS)

        assert action.action_type == ActionType.SUPPRESS


class TestProfileState:
    def test_disabled_state(self):
        profile = ProfileConfig(name="Test", enabled=False)
        assert profile.state == ProfileState.INACTIVE

    def test_permanent_standby_state(self):
        profile = ProfileConfig(name="Test", enabled=True, is_permanent=True)
        assert profile.state == ProfileState.STANDBY

    def test_conditional_waiting_state(self):
        profile = ProfileConfig(
            name="Test",
            enabled=True,
            is_permanent=False,
            window_rules=[WindowRule(field="class", pattern="cs2")],
        )
        assert profile.state == ProfileState.WAITING


class TestProtectedButtons:
    def test_left_click_protected(self):
        assert is_protected_button("btn_left") is True
        assert is_protected_button("BTN_LEFT") is True

    def test_right_click_protected(self):
        assert is_protected_button("btn_right") is True

    def test_middle_click_not_protected(self):
        assert is_protected_button("btn_middle") is False

    def test_other_buttons_not_protected(self):
        assert is_protected_button("btn_back") is False
        assert is_protected_button("btn_forward") is False
