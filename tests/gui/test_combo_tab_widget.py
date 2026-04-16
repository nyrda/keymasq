# ruff: noqa: F403, F405, I001
from tests.gui.support import *

class TestComboTabWidget:
    def test_combo_tab_does_not_start_active_profile_polling(self, monkeypatch):
        from keymasq.gui.widgets.combo_tab import ComboTab
        from keymasq.gui.widgets import profile_managed_tab as profile_tab_module

        def fail_request(*args, **kwargs):
            raise AssertionError("ComboTab should not request active profiles during construction")

        monkeypatch.setattr(profile_tab_module, "session_request_async", fail_request)

        ComboTab(profile_manager=None, demo_mode=False)

    def test_combo_tab_syncs_with_device_tab_selection(self, temp_config_dir):
        from keymasq.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
        )
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        window.profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
                enabled=True,
                is_permanent=True,
                device_layers={"2234:6678": DeviceProfileLayer(hardware_id="2234:6678")},
            )
        )
        window.profile_manager.save_profile(
            ProfileConfig(
                name="Gaming",
                enabled=True,
                is_permanent=True,
                device_layers={"2234:6678": DeviceProfileLayer(hardware_id="2234:6678")},
            )
        )

        device = HardwareConfig(
            vendor_id="2234",
            product_id="6678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        window._add_device_tab(device)
        tab = window.stack.get_page(window.stack.get_child_by_name(device.hardware_id)).get_child()

        tab.profile_dropdown.set_selected(tab._profile_names.index("Gaming"))

        assert window.combo_tab is not None
        assert window.combo_tab._selected_profile is not None
        assert window.combo_tab._selected_profile.config.name == "Gaming"

    def test_combo_tab_profile_selection_syncs_back_to_device_tabs(self, temp_config_dir):
        from keymasq.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
        )
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        window.profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
                enabled=True,
                is_permanent=True,
                device_layers={"2234:6678": DeviceProfileLayer(hardware_id="2234:6678")},
            )
        )
        window.profile_manager.save_profile(
            ProfileConfig(
                name="Gaming",
                enabled=True,
                is_permanent=True,
                device_layers={"2234:6678": DeviceProfileLayer(hardware_id="2234:6678")},
            )
        )

        device = HardwareConfig(
            vendor_id="2234",
            product_id="6678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        window._add_device_tab(device)
        tab = window.stack.get_page(window.stack.get_child_by_name(device.hardware_id)).get_child()

        assert window.combo_tab is not None
        window.combo_tab.profile_dropdown.set_selected(
            window.combo_tab._profile_names.index("Gaming")
        )

        assert window._selected_profile_name == "Gaming"
        assert tab._selected_profile is not None
        assert tab._selected_profile.config.name == "Gaming"

    def test_combo_tab_add_edit_delete_combo(self, temp_config_dir):
        from keymasq.common.models import (
            ActionType,
            ComboConfig,
            ComboEvent,
            ComboStep,
            MappingAction,
            ProfileConfig,
        )
        from keymasq.gui.widgets.combo_tab import ComboTab
        from keymasq.session.profiles import ProfileManager

        profile_manager = ProfileManager()
        profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
                enabled=True,
                is_permanent=True,
            )
        )

        tab = ComboTab(profile_manager=profile_manager, demo_mode=False)
        tab.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)

        combo = ComboConfig(
            id="combo-1",
            name="Quick Save",
            steps=[
                ComboStep(
                    events=[
                        ComboEvent(
                            evdev="key_leftctrl",
                            hardware_id="1234:5678",
                            source="kbd",
                        ),
                        ComboEvent(evdev="key_s", hardware_id="1234:5678", source="kbd"),
                    ]
                )
            ],
            action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
        )
        tab._on_combo_saved(None, combo)

        assert len(tab._selected_combos()) == 1
        assert tab.combo_listbox.get_first_child() is not None
        reloaded = profile_manager.get_profile("Desktop")
        assert reloaded is not None
        assert reloaded.config.combos[0].steps[0].events[0].hardware_id == "1234:5678"
        reloaded_manager = ProfileManager()
        persisted = reloaded_manager.get_profile("Desktop")
        assert persisted is not None
        assert persisted.config.combos[0].steps[0].events[0].hardware_id == "1234:5678"

        updated = ComboConfig(
            id=combo.id,
            name="Quick Load",
            steps=[
                ComboStep(
                    events=[
                        ComboEvent(
                            evdev="key_leftctrl",
                            hardware_id="1234:5678",
                            source="kbd",
                        ),
                        ComboEvent(evdev="key_l", hardware_id="1234:5678", source="kbd"),
                    ]
                )
            ],
            action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f9"),
        )
        tab._on_combo_saved(None, updated)

        assert len(tab._selected_combos()) == 1
        assert tab._selected_combos()[0].name == "Quick Load"

        tab._on_delete_combo_clicked(None, combo.id)

        assert tab._selected_combos() == []
        assert tab.section_label.get_text() == "No combos in this profile."

    def test_combo_tab_marks_active_profile_from_session_payload(self, temp_config_dir):
        from keymasq.common.models import ProfileConfig
        from keymasq.gui.widgets.combo_tab import ComboTab
        from keymasq.session.profiles import ProfileManager

        profile_manager = ProfileManager()
        profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
                enabled=True,
                is_permanent=True,
            )
        )

        tab = ComboTab(profile_manager=profile_manager, demo_mode=True)
        tab.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)
        tab._on_active_profile_response({"active_profiles": ["Desktop"]})

        assert tab._active_profile_names == ["Desktop"]
        assert tab.status_label.get_text() == "active"

    def test_combo_tab_respects_compositor_tag_rule_capability(self, temp_config_dir):
        from keymasq.common.models import ProfileConfig, WindowRule
        from keymasq.gui.widgets.combo_tab import ComboTab
        from keymasq.session.profiles import ProfileManager

        profile_manager = ProfileManager()
        profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
                enabled=True,
                is_permanent=False,
                window_rules=[WindowRule(field="tag", pattern="work")],
            )
        )

        unsupported = ComboTab(profile_manager=profile_manager, demo_mode=True)
        unsupported.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)

        supported = ComboTab(
            profile_manager=profile_manager,
            demo_mode=True,
            compositor_capabilities=["window_tags"],
        )
        supported.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)

        assert unsupported.status_label.get_text() == "unsupported rules"
        assert supported.status_label.get_text() == "waiting"

    def test_combo_tab_empty_state_uses_section_header_text(self, temp_config_dir):
        from keymasq.common.models import ProfileConfig
        from keymasq.gui.widgets.combo_tab import ComboTab
        from keymasq.session.profiles import ProfileManager

        profile_manager = ProfileManager()
        profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
                enabled=True,
                is_permanent=True,
            )
        )

        tab = ComboTab(profile_manager=profile_manager, demo_mode=True)
        tab.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)

        assert tab.section_label.get_text() == "No combos in this profile."
        assert tab.combo_listbox.get_visible() is False

    def test_combo_tab_sorts_rows_and_opens_editor_for_activated_row(self, temp_config_dir):
        from gi.repository import Gtk

        from keymasq.common.models import (
            ActionType,
            ComboConfig,
            ComboEvent,
            ComboStep,
            MappingAction,
            ProfileConfig,
        )
        from keymasq.gui.widgets.combo_tab import ComboTab
        from keymasq.session.profiles import ProfileManager

        def combo(combo_id: str, name: str, trigger_key: str, action_key: str) -> ComboConfig:
            return ComboConfig(
                id=combo_id,
                name=name,
                steps=[
                    ComboStep(
                        events=[
                            ComboEvent(
                                evdev=trigger_key,
                                hardware_id="1234:5678",
                                source="kbd",
                            )
                        ]
                    )
                ],
                action=MappingAction(action_type=ActionType.KEYBOARD, target=action_key),
            )

        profile_manager = ProfileManager()
        profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
                enabled=True,
                is_permanent=True,
                combos=[
                    combo("combo-c", "Charlie", "key_c", "key_3"),
                    combo("combo-a", "alpha", "key_a", "key_1"),
                    combo("combo-b", "Bravo", "key_b", "key_2"),
                ],
            )
        )

        tab = ComboTab(profile_manager=profile_manager, demo_mode=True)
        tab.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)

        tab._on_column_header_clicked(tab._name_header_btn, 1)
        name_rows = []
        row = tab.combo_listbox.get_first_child()
        while row is not None:
            name_rows.append(row.get_child().get_first_child().get_label())
            row = row.get_next_sibling()

        tab._on_column_header_clicked(tab._name_header_btn, 1)
        reversed_rows = []
        row = tab.combo_listbox.get_first_child()
        while row is not None:
            reversed_rows.append(row.get_child().get_first_child().get_label())
            row = row.get_next_sibling()

        opened: list[str] = []
        tab._open_combo_editor = lambda selected=None: opened.append(
            selected.id if selected else "new"
        )
        first_row = tab.combo_listbox.get_first_child()
        missing_row = Gtk.ListBoxRow()
        missing_row._combo_id = "missing"  # type: ignore[attr-defined]

        tab._on_row_activated(tab.combo_listbox, first_row)
        tab._on_row_activated(tab.combo_listbox, missing_row)

        assert name_rows == ["alpha", "Bravo", "Charlie"]
        assert reversed_rows == ["Charlie", "Bravo", "alpha"]
        assert tab._name_header_btn.get_label() == "Name ▾"
        assert opened == ["combo-c"]

    def test_combo_tab_add_combo_requires_selected_profile(self, temp_config_dir):
        from gi.repository import Gtk

        from keymasq.gui.widgets.combo_tab import ComboTab

        tab = ComboTab(profile_manager=None, demo_mode=True)
        opened: list[str] = []
        tab._open_combo_editor = lambda combo=None: opened.append("opened")

        tab._on_add_combo_clicked(Gtk.Button())

        assert tab.section_label.get_text() == "Combos"
        assert tab._selected_profile is None
        assert opened == []
