import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import pytest
import tomli_w

from keymasq.common import config_files as config_files_module
from keymasq.common.models import (
    ActionType,
    ComboConfig,
    ComboEvent,
    ComboStep,
    DeviceProfileLayer,
    MappingAction,
    ProfileConfig,
    ProfileDeactivationPolicy,
    WindowRule,
)
from keymasq.session.profiles import ProfileManager


def _write_profile_toml(
    config_dir: Path,
    filename: str,
    *,
    name: str,
    enabled: bool = True,
    is_permanent: bool = True,
    priority: int = 0,
    notify_on_activation: bool = True,
    created_at: str = "2026-05-18T12:00:00",
    extra_sections: str = "",
) -> Path:
    path = config_dir / "profiles" / filename
    content = tomli_w.dumps(
        {
            "profile": {
                "name": name,
                "enabled": enabled,
                "is_permanent": is_permanent,
                "priority": priority,
                "notify_on_activation": notify_on_activation,
                "created_at": created_at,
            }
        }
    )
    if extra_sections:
        content = f"{content}\n{extra_sections.strip()}\n"

    path.write_text(content, encoding="utf-8")
    return path


class TestProfileManager:
    def test_auto_create_default_profile_seeds_editable_startup_profile(self, temp_config_dir):
        manager = ProfileManager(auto_create_default_if_empty=True)

        profiles = manager.list_profiles()

        assert len(profiles) == 1
        assert profiles[0].config.name == "Default"
        assert profiles[0].config.enabled is True
        assert profiles[0].config.is_permanent is True
        assert profiles[0].config.notify_on_activation is False
        assert profiles[0].path == temp_config_dir / "profiles" / "Default.toml"
        assert profiles[0].path.exists()

    def test_save_and_load_profile(self, temp_config_dir, sample_profile_config):
        manager = ProfileManager()
        manager.save_profile(sample_profile_config)

        profiles = manager.list_profiles()

        assert len(profiles) == 1
        assert profiles[0].config.name == sample_profile_config.name
        assert len(profiles[0].config.device_layers["1234:5678"].mappings) == 2

    def test_save_and_load_profile_lifecycle_macros(self, temp_config_dir):
        manager = ProfileManager()
        manager.save_profile(
            ProfileConfig(
                name="Gaming",
                enabled=True,
                activation_macro_name="game_enter",
                deactivation_macro_name="game_leave",
            )
        )

        reloaded = ProfileManager()
        profile = reloaded.get_profile("Gaming")

        assert profile is not None
        assert profile.config.activation_macro_name == "game_enter"
        assert profile.config.deactivation_macro_name == "game_leave"
        content = profile.path.read_text(encoding="utf-8")
        assert 'activation_macro = "game_enter"' in content
        assert 'deactivation_macro = "game_leave"' in content

    def test_profile_failed_overwrite_preserves_existing_file_and_state(
        self,
        temp_config_dir,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = ProfileManager()
        manager.save_profile(ProfileConfig(name="Saved Profile", enabled=True))
        saved = manager.get_profile("Saved Profile")
        assert saved is not None
        original_content = saved.path.read_bytes()

        def fail_dump(_data: object, config_file: BinaryIO) -> None:
            config_file.write(b'[profile]\nname = "partial"\n')
            raise OSError("disk full")

        monkeypatch.setattr(config_files_module.tomli_w, "dump", fail_dump)

        with pytest.raises(OSError, match="disk full"):
            manager.save_profile(
                ProfileConfig(name="Saved Profile", enabled=False),
                path=saved.path,
            )

        loaded = manager.get_profile("Saved Profile")
        assert saved.path.read_bytes() == original_content
        assert loaded is not None
        assert loaded.config.enabled is True
        assert list((temp_config_dir / "profiles").glob(f".{saved.path.name}.*")) == []

    def test_profile_macro_action_accepts_macro_name_alias(self, temp_config_dir):
        _write_profile_toml(
            temp_config_dir,
            "macro-alias.toml",
            name="Macro Alias",
            is_permanent=False,
            created_at="2026-05-29T12:00:00",
            extra_sections="""
[devices."1234:5678"]
always_grab_all = false

[devices."1234:5678".mapping.btn_side]
action = "macro"
macro_name = "Example"
""",
        )

        manager = ProfileManager()
        profile = manager.get_profile("Macro Alias")

        assert profile is not None
        action = profile.config.device_layers["1234:5678"].mappings["btn_side"]
        assert action.action_type == ActionType.MACRO
        assert action.macro_name == "Example"

        manager.save_profile(profile.config)

        content = profile.path.read_text(encoding="utf-8")
        assert 'target = "Example"' in content
        assert 'macro_name = "Example"' in content

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

    def test_profile_action_deactivation_policy_round_trips(self, temp_config_dir):
        manager = ProfileManager()
        manager.save_profile(
            ProfileConfig(
                name="Launcher",
                enabled=True,
                is_permanent=True,
                device_layers={
                    "1234:5678": DeviceProfileLayer(
                        hardware_id="1234:5678",
                        mappings={
                            "btn_back": MappingAction(
                                action_type=ActionType.PROFILE_ENABLE,
                                profile_name="Nav Layer",
                                profile_deactivation=ProfileDeactivationPolicy(
                                    on_trigger_end=True,
                                    after_actions=1,
                                    timeout_ms=1500,
                                ),
                            ),
                            "btn_side": MappingAction(
                                action_type=ActionType.PROFILE_TOGGLE,
                                profile_name="Nav Layer",
                                profile_deactivation=ProfileDeactivationPolicy(
                                    on_trigger_end=True,
                                    after_actions=2,
                                    timeout_ms=2000,
                                ),
                            ),
                            "btn_middle": MappingAction(
                                action_type=ActionType.PROFILE_DISABLE,
                                profile_name="Nav Layer",
                                profile_deactivation=ProfileDeactivationPolicy(
                                    on_trigger_end=True
                                ),
                            ),
                        },
                    )
                },
            )
        )

        reloaded = ProfileManager()
        loaded = reloaded.get_profile("Launcher")

        assert loaded is not None
        action = loaded.config.device_layers["1234:5678"].mappings["btn_back"]
        assert action.profile_deactivation == ProfileDeactivationPolicy(
            on_trigger_end=True,
            after_actions=1,
            timeout_ms=1500,
        )
        toggle = loaded.config.device_layers["1234:5678"].mappings["btn_side"]
        assert toggle.profile_deactivation == ProfileDeactivationPolicy(
            on_trigger_end=True,
            after_actions=2,
            timeout_ms=2000,
        )
        disabled = loaded.config.device_layers["1234:5678"].mappings["btn_middle"]
        assert disabled.profile_deactivation is None
        content = loaded.path.read_text(encoding="utf-8")
        assert "deactivation" in content
        assert "on_trigger_end = true" in content
        assert "after_actions = 1" in content
        assert "timeout_ms = 1500" in content
        assert "after_actions = 2" in content
        assert "timeout_ms = 2000" in content

    def test_runtime_profile_ordering_appends_overlays(self, temp_config_dir):
        manager = ProfileManager()
        for name, enabled, key in (
            ("Base", True, "key_1"),
            ("Overlay", True, "key_2"),
            ("Runtime", False, "key_3"),
        ):
            manager.save_profile(
                ProfileConfig(
                    name=name,
                    enabled=enabled,
                    is_permanent=True,
                    priority=1,
                    created_at=datetime.now(),
                    device_layers={
                        "1234:5678": DeviceProfileLayer(
                            hardware_id="1234:5678",
                            mappings={
                                "btn_back": MappingAction(
                                    action_type=ActionType.KEYBOARD,
                                    target=key,
                                )
                            },
                        )
                    },
                )
            )

        resolved = manager.resolve_active_profiles(
            hardware_ids=["1234:5678"],
            runtime_profile_names=["Base", "Runtime"],
        )

        assert [profile.name for profile in resolved.active_profiles] == [
            "Overlay",
            "Base",
            "Runtime",
        ]
        device = resolved.devices["1234:5678"]
        assert device.active_profile_names == ["Overlay", "Base", "Runtime"]
        assert device.mappings["btn_back"].target == "key_3"
        assert device.mappings["btn_back"].source_profile_name == "Runtime"

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

    def test_optional_combo_hardware_id_round_trips_without_grab_inference(
        self,
        temp_config_dir,
    ):
        manager = ProfileManager()
        profile = ProfileConfig(
            name="Portable Combo",
            enabled=True,
            is_permanent=True,
            combos=[
                ComboConfig(
                    id="combo-any-kbd",
                    name="Any Keyboard F13",
                    steps=[
                        ComboStep(
                            events=[
                                ComboEvent(
                                    source="kbd",
                                    evdev="key_f13",
                                )
                            ]
                        )
                    ],
                    action=MappingAction(action_type=ActionType.SUPPRESS),
                )
            ],
        )
        manager.save_profile(profile)

        text = (temp_config_dir / "profiles" / "Portable_Combo.toml").read_text(
            encoding="utf-8"
        )
        assert "hardware_id" not in text

        reloaded = ProfileManager()
        loaded = reloaded.get_profile("Portable Combo")
        assert loaded is not None
        event = loaded.config.combos[0].steps[0].events[0]
        assert event.hardware_id == ""

        resolved = reloaded.resolve_active_profiles(hardware_ids=["1234:5678"])

        assert len(resolved.combos) == 1
        assert resolved.combos[0].steps[0].events[0].hardware_id == ""
        assert resolved.combos[0].steps[0].events[0].source == "kbd"
        assert resolved.devices["1234:5678"].combo_event_count == 0

    def test_match_across_devices_preserves_storage_scope_but_strips_runtime_scope(
        self,
        temp_config_dir,
    ):
        manager = ProfileManager()
        profile = ProfileConfig(
            name="Portable Captured Combo",
            enabled=True,
            is_permanent=True,
            combos=[
                ComboConfig(
                    id="combo-portable",
                    name="Portable F13",
                    match_across_devices=True,
                    steps=[
                        ComboStep(
                            events=[
                                ComboEvent(
                                    hardware_id="1234:5678",
                                    source="kbd",
                                    evdev="key_f13",
                                )
                            ]
                        )
                    ],
                    action=MappingAction(action_type=ActionType.SUPPRESS),
                )
            ],
        )
        manager.save_profile(profile)

        text = (
            temp_config_dir / "profiles" / "Portable_Captured_Combo.toml"
        ).read_text(encoding="utf-8")
        assert "match_across_devices = true" in text
        assert 'hardware_id = "1234:5678"' in text
        assert 'source = "kbd"' in text

        reloaded = ProfileManager()
        loaded = reloaded.get_profile("Portable Captured Combo")
        assert loaded is not None
        stored_combo = loaded.config.combos[0]
        stored_event = stored_combo.steps[0].events[0]
        assert stored_combo.match_across_devices is True
        assert stored_event.hardware_id == "1234:5678"
        assert stored_event.source == "kbd"

        resolved = reloaded.resolve_active_profiles(hardware_ids=["1234:5678"])

        assert len(resolved.combos) == 1
        runtime_event = resolved.combos[0].steps[0].events[0]
        assert runtime_event.hardware_id == ""
        assert runtime_event.source is None
        assert resolved.devices["1234:5678"].combo_event_count == 0
        assert resolved.devices["1234:5678"].combo_sources == set()

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
                    recall_trigger_keys=True,
                    restore_trigger_keys=["ctrl"],
                )
            ],
        )
        manager.save_profile(profile)

        loaded = manager.get_profile("Combo Timeout")

        assert loaded is not None
        assert loaded.config.combos[0].steps[0].timeout_ms is None
        assert loaded.config.combos[0].steps[1].timeout_ms == 750
        assert loaded.config.combos[0].recall_trigger_keys is True
        assert loaded.config.combos[0].restore_trigger_keys == ["ctrl"]

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

    def test_save_existing_loaded_profile_updates_original_path(self, temp_config_dir):
        profiles_dir = temp_config_dir / "profiles"
        fixture_path = _write_profile_toml(
            temp_config_dir,
            "analog-gamepad.toml",
            name="Integration Analog Gamepad",
        )

        manager = ProfileManager()
        profile = manager.get_profile("Integration Analog Gamepad")

        assert profile is not None
        assert profile.path == fixture_path

        manager.set_profile_enabled("Integration Analog Gamepad", False)

        assert 'enabled = false' in fixture_path.read_text(encoding="utf-8")
        assert not (profiles_dir / "Integration_Analog_Gamepad.toml").exists()

    def test_set_profile_enabled_serializes_against_reload(
        self,
        temp_config_dir,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = ProfileManager()
        manager.save_profile(ProfileConfig(name="Race Profile", enabled=True))
        original_save_profile = manager.save_profile
        save_started = threading.Event()
        release_save = threading.Event()

        def save_profile_with_pause(
            config: ProfileConfig,
            path: Path | None = None,
        ) -> None:
            save_started.set()
            assert release_save.wait(timeout=5)
            original_save_profile(config, path=path)

        monkeypatch.setattr(manager, "save_profile", save_profile_with_pause)
        setter_errors: list[object] = []
        reload_errors: list[object] = []

        def set_profile_disabled() -> None:
            try:
                manager.set_profile_enabled("Race Profile", False)
            except (AssertionError, OSError, RuntimeError, ValueError) as exc:
                setter_errors.append(exc)

        setter = threading.Thread(target=set_profile_disabled)
        reloader: threading.Thread | None = None
        setter.start()
        try:
            assert save_started.wait(timeout=5)
            original_load_all = manager._load_all
            reload_started = threading.Event()
            reload_reached_load = threading.Event()

            def load_all_with_signal(*, strict: bool = False) -> None:
                reload_reached_load.set()
                original_load_all(strict=strict)

            def reload_profiles() -> None:
                reload_started.set()
                try:
                    manager.reload()
                except (AssertionError, OSError, RuntimeError, ValueError) as exc:
                    reload_errors.append(exc)

            monkeypatch.setattr(manager, "_load_all", load_all_with_signal)
            reloader = threading.Thread(target=reload_profiles)
            reloader.start()

            assert reload_started.wait(timeout=5)
            assert not reload_reached_load.wait(timeout=0.2)
        finally:
            release_save.set()
            setter.join(timeout=5)
            if reloader is not None:
                reloader.join(timeout=5)

        assert not setter.is_alive()
        assert reloader is not None
        assert not reloader.is_alive()
        assert setter_errors == []
        assert reload_errors == []
        profile = manager.get_profile("Race Profile")
        assert profile is not None
        assert profile.config.enabled is False

    def test_profile_reads_do_not_wait_for_reload_disk_load(
        self,
        temp_config_dir,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = ProfileManager()
        manager.save_profile(ProfileConfig(name="Race Profile", enabled=True, is_permanent=True))
        original_load_all = manager._load_all
        reload_reached_load = threading.Event()
        release_reload = threading.Event()
        reload_errors: list[object] = []

        def load_all_with_pause(*, strict: bool = False) -> None:
            reload_reached_load.set()
            assert release_reload.wait(timeout=5)
            original_load_all(strict=strict)

        def reload_profiles() -> None:
            try:
                manager.reload()
            except (AssertionError, OSError, RuntimeError, ValueError) as exc:
                reload_errors.append(exc)

        monkeypatch.setattr(manager, "_load_all", load_all_with_pause)
        reloader = threading.Thread(target=reload_profiles)
        reloader.start()

        read_done = threading.Event()
        read_errors: list[object] = []
        reader: threading.Thread | None = None
        try:
            assert reload_reached_load.wait(timeout=5)

            def read_profiles() -> None:
                try:
                    profile = manager.get_profile("Race Profile")
                    assert profile is not None
                    assert [info.config.name for info in manager.list_profiles()] == [
                        "Race Profile"
                    ]
                    resolved = manager.resolve_active_profiles()
                    assert [profile.name for profile in resolved.active_profiles] == [
                        "Race Profile"
                    ]
                except (AssertionError, OSError, RuntimeError, ValueError) as exc:
                    read_errors.append(exc)
                finally:
                    read_done.set()

            reader = threading.Thread(target=read_profiles)
            reader.start()
            assert read_done.wait(timeout=0.2)
        finally:
            release_reload.set()
            if reader is not None:
                reader.join(timeout=5)
            reloader.join(timeout=5)

        assert reader is not None
        assert not reader.is_alive()
        assert not reloader.is_alive()
        assert read_errors == []
        assert reload_errors == []

    def test_duplicate_profile_names_prefer_canonical_path(
        self,
        temp_config_dir,
        caplog,
    ):
        canonical_path = _write_profile_toml(
            temp_config_dir,
            "Integration_Analog_Gamepad.toml",
            name="Integration Analog Gamepad",
            enabled=False,
        )
        _write_profile_toml(
            temp_config_dir,
            "analog-gamepad.toml",
            name="Integration Analog Gamepad",
        )

        manager = ProfileManager()
        profile = manager.get_profile("Integration Analog Gamepad")

        assert profile is not None
        assert profile.path == canonical_path
        assert profile.config.enabled is False
        assert len(manager.list_profiles()) == 1
        assert "Ignoring duplicate profile name 'Integration Analog Gamepad'" in caplog.text

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
            "keymasq.session.profiles.MAX_PROFILE_PATH_ATTEMPTS",
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

    def test_created_at_repair_logs_unexpected_failure(
        self,
        temp_config_dir,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
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

        def fail_repair(
            self: ProfileManager,
            created_at: datetime,
            path: Path,
        ) -> None:
            raise RuntimeError("repair bug")

        monkeypatch.setattr(ProfileManager, "_repair_created_at_if_needed", fail_repair)

        with caplog.at_level("ERROR", logger="keymasq-session.profiles"):
            manager = ProfileManager()

        assert manager.get_profile("Missing Created") is not None
        assert f"Unexpected failure repairing created_at for {profile_path}" in caplog.text
        assert "repair bug" in caplog.text

    def test_pending_created_at_repair_does_not_overwrite_newer_profile_save(
        self,
        temp_config_dir,
    ):
        profile_path = Path(temp_config_dir) / "profiles" / "missing-created-at.toml"
        profile_path.write_text(
            """
[profile]
name = "Missing Created"
enabled = true
is_permanent = true
priority = 1
notify_on_activation = true

[devices."1234:5678"]
always_grab_all = false

[devices."1234:5678".mapping.btn_back]
action = "keyboard"
target = "key_1"
""".strip(),
            encoding="utf-8",
        )
        manager = ProfileManager()
        loaded = manager.get_profile("Missing Created")
        assert loaded is not None

        layer = loaded.config.device_layers["1234:5678"]
        layer.mappings["btn_forward"] = MappingAction(
            action_type=ActionType.KEYBOARD,
            target="key_2",
        )
        manager.save_profile(loaded.config)

        manager._repair_created_at_if_needed(
            loaded.config.created_at or datetime.now(),
            profile_path,
        )

        reloaded = ProfileManager()
        saved = reloaded.get_profile("Missing Created")
        assert saved is not None
        mappings = saved.config.device_layers["1234:5678"].mappings
        assert mappings["btn_back"].target == "key_1"
        assert mappings["btn_forward"].target == "key_2"

    def test_delete_profile_falls_back_to_unlink_when_trash_move_fails(
        self,
        temp_config_dir,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        manager = ProfileManager()
        manager.save_profile(ProfileConfig(name="Delete Me"))
        profile = manager.get_profile("Delete Me")
        assert profile is not None
        profile_path = profile.path
        original_rename = Path.rename

        def fail_profile_rename(self: Path, target: Path) -> Path:
            if self == profile_path:
                raise OSError("cross-device link")
            return original_rename(self, target)

        monkeypatch.setattr(Path, "rename", fail_profile_rename)

        with caplog.at_level("WARNING", logger="keymasq-session.profiles"):
            deleted = manager.delete_profile("Delete Me")

        assert deleted is True
        assert manager.get_profile("Delete Me") is None
        assert not profile_path.exists()
        assert "Failed to move deleted profile to trash" in caplog.text
        assert "cross-device link" in caplog.text

    def test_delete_profile_does_not_unlink_on_unexpected_trash_failure(
        self,
        temp_config_dir,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = ProfileManager()
        manager.save_profile(ProfileConfig(name="Delete Me"))
        profile = manager.get_profile("Delete Me")
        assert profile is not None
        profile_path = profile.path

        def fail_profile_rename(self: Path, target: Path) -> Path:
            if self == profile_path:
                raise RuntimeError("rename bug")
            raise AssertionError("Unexpected rename call")

        monkeypatch.setattr(Path, "rename", fail_profile_rename)

        with pytest.raises(RuntimeError, match="rename bug"):
            manager.delete_profile("Delete Me")

        assert manager.get_profile("Delete Me") is profile
        assert profile_path.exists()

    def test_delete_profile_preserves_state_when_fallback_unlink_fails(
        self,
        temp_config_dir,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        manager = ProfileManager()
        manager.save_profile(ProfileConfig(name="Delete Me"))
        profile = manager.get_profile("Delete Me")
        assert profile is not None
        profile_path = profile.path
        original_rename = Path.rename
        original_unlink = Path.unlink

        def fail_profile_rename(self: Path, target: Path) -> Path:
            if self == profile_path:
                raise OSError("cross-device link")
            return original_rename(self, target)

        def fail_profile_unlink(self: Path, missing_ok: bool = False) -> None:
            if self == profile_path:
                raise OSError("permission denied")
            original_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "rename", fail_profile_rename)
        monkeypatch.setattr(Path, "unlink", fail_profile_unlink)

        with (
            caplog.at_level("WARNING", logger="keymasq-session.profiles"),
            pytest.raises(OSError, match="permission denied"),
        ):
            manager.delete_profile("Delete Me")

        assert manager.get_profile("Delete Me") is profile
        assert profile_path.exists()
        assert "Failed to move deleted profile to trash" in caplog.text
        assert "Failed to delete profile file" in caplog.text

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
