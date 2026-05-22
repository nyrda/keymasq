# ruff: noqa: F403, F405, I001
from tests.common.support import *

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
        fixture_path = profiles_dir / "analog-gamepad.toml"
        fixture_path.write_text(
            """
[profile]
name = "Integration Analog Gamepad"
enabled = true
is_permanent = true
priority = 0
notify_on_activation = true
created_at = "2026-05-18T12:00:00"
""".lstrip(),
            encoding="utf-8",
        )

        manager = ProfileManager()
        profile = manager.get_profile("Integration Analog Gamepad")

        assert profile is not None
        assert profile.path == fixture_path

        manager.set_profile_enabled("Integration Analog Gamepad", False)

        assert 'enabled = false' in fixture_path.read_text(encoding="utf-8")
        assert not (profiles_dir / "Integration_Analog_Gamepad.toml").exists()

    def test_duplicate_profile_names_prefer_canonical_path(
        self,
        temp_config_dir,
        caplog,
    ):
        profiles_dir = temp_config_dir / "profiles"
        canonical_path = profiles_dir / "Integration_Analog_Gamepad.toml"
        duplicate_path = profiles_dir / "analog-gamepad.toml"
        canonical_path.write_text(
            """
[profile]
name = "Integration Analog Gamepad"
enabled = false
is_permanent = true
priority = 0
notify_on_activation = true
created_at = "2026-05-18T12:00:00"
""".lstrip(),
            encoding="utf-8",
        )
        duplicate_path.write_text(
            """
[profile]
name = "Integration Analog Gamepad"
enabled = true
is_permanent = true
priority = 0
notify_on_activation = true
created_at = "2026-05-18T12:00:00"
""".lstrip(),
            encoding="utf-8",
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
