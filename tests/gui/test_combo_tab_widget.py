# ruff: noqa: I001
import pytest

from tests.gui.support import collect_listbox_row_labels

pytest.importorskip("gi")



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
        tab = window._child_for_hardware_id(device.hardware_id)

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
        tab = window._child_for_hardware_id(device.hardware_id)

        assert window.combo_tab is not None
        window.combo_tab.profile_dropdown.set_selected(
            window.combo_tab._profile_names.index("Gaming")
        )

        assert window._selected_profile_name == "Gaming"
        assert tab._selected_profile is not None
        assert tab._selected_profile.config.name == "Gaming"

    def test_combo_tab_add_edit_delete_combo(self, temp_config_dir, monkeypatch):
        from keymasq.common.models import (
            ActionType,
            ComboConfig,
            ComboEvent,
            ComboStep,
            MappingAction,
            ProfileConfig,
        )
        import keymasq.gui.widgets.combo_tab as combo_tab_module
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

        presented: list[object] = []
        monkeypatch.setattr(
            combo_tab_module.Adw.AlertDialog,
            "present",
            lambda dialog, parent: presented.append((dialog, parent)),
        )

        tab._on_delete_combo_clicked(None, combo.id)

        assert len(presented) == 1
        assert len(tab._selected_combos()) == 1

        tab._on_delete_combo_response(None, "delete", combo.id)

        assert tab._selected_combos() == []
        assert tab.section_label.get_text() == "No combos in this profile."

    def test_combo_tab_rejects_stale_duplicate_combo_save(self, temp_config_dir, monkeypatch):
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

        def combo(combo_id: str, name: str) -> ComboConfig:
            return ComboConfig(
                id=combo_id,
                name=name,
                steps=[
                    ComboStep(
                        events=[
                            ComboEvent(
                                evdev="key_s",
                                hardware_id="1234:5678",
                                source="kbd",
                            )
                        ]
                    )
                ],
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
            )

        profile_manager = ProfileManager()
        profile_manager.save_profile(ProfileConfig(name="Desktop"))

        tab = ComboTab(profile_manager=profile_manager, demo_mode=True)
        tab.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)
        assert tab._selected_profile is not None
        errors: list[str] = []
        monkeypatch.setattr(tab, "_show_profile_error_dialog", errors.append)

        target_profile = tab._selected_profile
        tab._on_combo_saved(None, combo("combo-1", "First"), target_profile)
        tab._on_combo_saved(None, combo("combo-2", "Second"), target_profile)

        assert [combo.id for combo in target_profile.config.combos] == ["combo-1"]
        assert errors == ["A combo with the same trigger already exists in this profile."]

    def test_combo_tab_save_uses_profile_selected_when_editor_opened(
        self,
        temp_config_dir,
        monkeypatch,
    ):
        from keymasq.common.models import (
            ActionType,
            ComboConfig,
            ComboEvent,
            ComboStep,
            MappingAction,
            ProfileConfig,
        )
        import keymasq.gui.widgets.combo_tab as combo_tab_module
        from keymasq.gui.widgets.combo_tab import ComboTab
        from keymasq.session.profiles import ProfileManager

        def combo(name: str) -> ComboConfig:
            return ComboConfig(
                id="shared-combo",
                name=name,
                steps=[
                    ComboStep(
                        events=[
                            ComboEvent(
                                evdev="key_s",
                                hardware_id="1234:5678",
                                source="kbd",
                            )
                        ]
                    )
                ],
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
            )

        class FakeComboEditorDialog:
            instances = []

            def __init__(
                self,
                _parent,
                _combo=None,
                profile_name=None,
                sibling_combos=None,
                emergency_cancel_combo_enabled=True,
            ):
                self.profile_name = profile_name
                self.sibling_combos = sibling_combos
                self.emergency_cancel_combo_enabled = emergency_cancel_combo_enabled
                self.handlers = []
                FakeComboEditorDialog.instances.append(self)

            def connect(self, signal, callback, *user_data):
                self.handlers.append((signal, callback, user_data))

            def present(self, _parent):
                pass

        monkeypatch.setattr(combo_tab_module, "ComboEditorDialog", FakeComboEditorDialog)

        profile_manager = ProfileManager()
        profile_manager.save_profile(ProfileConfig(name="Desktop", combos=[combo("Desktop")]))
        profile_manager.save_profile(ProfileConfig(name="Gaming", combos=[combo("Gaming")]))

        tab = ComboTab(profile_manager=profile_manager, demo_mode=True)
        tab.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)
        desktop_combo = profile_manager.get_profile("Desktop").config.combos[0]

        tab._open_combo_editor(desktop_combo)

        dialog = FakeComboEditorDialog.instances[0]
        assert dialog.profile_name == "Desktop"
        assert [combo.name for combo in dialog.sibling_combos] == ["Desktop"]

        tab.refresh_profiles(preferred_profile_name="Gaming", publish_selection=False)
        signal, callback, user_data = dialog.handlers[0]
        assert signal == "combo-saved"

        callback(None, combo("Desktop Updated"), *user_data)

        assert profile_manager.get_profile("Desktop").config.combos[0].name == "Desktop Updated"
        assert profile_manager.get_profile("Gaming").config.combos[0].name == "Gaming"
        assert tab._selected_profile.config.name == "Gaming"

    def test_combo_tab_delete_uses_profile_selected_when_dialog_opened(
        self,
        temp_config_dir,
        monkeypatch,
    ):
        from keymasq.common.models import (
            ActionType,
            ComboConfig,
            ComboEvent,
            ComboStep,
            MappingAction,
            ProfileConfig,
        )
        import keymasq.gui.widgets.combo_tab as combo_tab_module
        from keymasq.gui.widgets.combo_tab import ComboTab
        from keymasq.session.profiles import ProfileManager

        def combo(name: str) -> ComboConfig:
            return ComboConfig(
                id="shared-combo",
                name=name,
                steps=[
                    ComboStep(
                        events=[
                            ComboEvent(
                                evdev="key_s",
                                hardware_id="1234:5678",
                                source="kbd",
                            )
                        ]
                    )
                ],
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
            )

        class FakeAlertDialog:
            instances = []

            def __init__(self):
                self.handlers = []
                FakeAlertDialog.instances.append(self)

            def set_heading(self, heading):
                self.heading = heading

            def set_body(self, body):
                self.body = body

            def add_response(self, *_args):
                pass

            def set_response_appearance(self, *_args):
                pass

            def set_default_response(self, *_args):
                pass

            def set_close_response(self, *_args):
                pass

            def connect(self, signal, callback, *user_data):
                self.handlers.append((signal, callback, user_data))

            def present(self, _parent):
                pass

        monkeypatch.setattr(combo_tab_module.Adw, "AlertDialog", FakeAlertDialog)

        profile_manager = ProfileManager()
        profile_manager.save_profile(ProfileConfig(name="Desktop", combos=[combo("Desktop")]))
        profile_manager.save_profile(ProfileConfig(name="Gaming", combos=[combo("Gaming")]))

        tab = ComboTab(profile_manager=profile_manager, demo_mode=True)
        tab.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)

        tab._on_delete_combo_clicked(None, "shared-combo")

        dialog = FakeAlertDialog.instances[0]
        assert dialog.heading == "Delete Combo"
        assert "Desktop" in dialog.body

        tab.refresh_profiles(preferred_profile_name="Gaming", publish_selection=False)
        signal, callback, user_data = dialog.handlers[0]
        assert signal == "response"

        callback(dialog, "delete", *user_data)

        assert profile_manager.get_profile("Desktop").config.combos == []
        assert [combo.name for combo in profile_manager.get_profile("Gaming").config.combos] == [
            "Gaming"
        ]
        assert tab._selected_profile.config.name == "Gaming"

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
        assert tab.active_profiles_label.get_text() == "Desktop"
        assert tab.active_profiles_label.get_tooltip_text() == "Layer order: Desktop"
        assert tab.status_label.get_text() == "active"

    def test_combo_tab_summarizes_layered_active_profiles(self, temp_config_dir):
        from keymasq.common.models import ProfileConfig
        from keymasq.gui.widgets.combo_tab import ComboTab
        from keymasq.session.profiles import ProfileManager

        profile_manager = ProfileManager()
        for name in ("Base", "App", "Game", "Overlay"):
            profile_manager.save_profile(ProfileConfig(name=name, enabled=True, is_permanent=True))

        tab = ComboTab(profile_manager=profile_manager, demo_mode=True)
        tab.refresh_profiles(preferred_profile_name="Base", publish_selection=False)
        tab._on_active_profile_response(
            {"active_profiles": ["Base", "App", "Game", "Overlay"]}
        )

        assert tab.active_profiles_label.get_text() == "Base, App, Game, +1"
        assert (
            tab.active_profiles_label.get_tooltip_text()
            == "Layer order: Base -> App -> Game -> Overlay"
        )

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

        tab._combo_list.name_header_btn.emit("clicked")
        name_rows = collect_listbox_row_labels(tab.combo_listbox)

        tab._combo_list.name_header_btn.emit("clicked")
        reversed_rows = collect_listbox_row_labels(tab.combo_listbox)

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
        assert tab._combo_list.name_header_btn.get_label() == "Name ▾"
        assert opened == ["combo-c"]

    def test_combo_tab_search_filters_rows(self, temp_config_dir):
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
                combos=[
                    ComboConfig(
                        id="combo-a",
                        name="Alpha",
                        steps=[
                            ComboStep(
                                events=[
                                    ComboEvent(
                                        evdev="key_a",
                                        hardware_id="1234:5678",
                                        source="kbd",
                                    )
                                ]
                            )
                        ],
                        action=MappingAction(action_type=ActionType.KEYBOARD, target="key_1"),
                    ),
                    ComboConfig(
                        id="combo-b",
                        name="Bravo",
                        steps=[
                            ComboStep(
                                events=[
                                    ComboEvent(
                                        evdev="btn_side",
                                        hardware_id="abcd:ef01",
                                        source="mouse",
                                    )
                                ]
                            )
                        ],
                        action=MappingAction(action_type=ActionType.MOUSE, target="btn_left"),
                    ),
                ],
            )
        )

        tab = ComboTab(profile_manager=profile_manager, demo_mode=True)
        tab.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)

        assert tab.search_entry.get_visible() is False
        tab.search_button.emit("clicked")
        assert tab.search_entry.get_visible() is True

        tab.search_entry.set_text("mouse")
        assert tab._combo_list.visible_count() == 1
        assert tab.section_label.get_visible() is False

        tab.search_entry.set_text("missing")
        assert tab._combo_list.visible_count() == 0
        assert tab.section_label.get_text() == "No matching combos."
        assert tab.combo_listbox.get_visible() is False

        tab._combo_list.hide_search()
        assert tab.search_entry.get_visible() is False
        assert tab.section_label.get_visible() is False

        tab.search_button.emit("clicked")
        tab.search_entry.set_text("mouse")
        tab._selected_profile = None
        tab._combo_list.render()
        assert tab.search_entry.get_visible() is False
        assert tab.search_entry.get_text() == ""

        tab._combo_list.show_search()
        assert tab.search_entry.get_visible() is False

        tab.search_entry.set_visible(True)
        tab.search_entry.set_text("stale")
        tab._combo_list.update_state()
        assert tab.search_entry.get_visible() is False
        assert tab.search_entry.get_text() == ""

    def test_combo_tab_toolbar_keeps_add_combo_and_search_left_bound(self):
        from keymasq.gui.widgets.combo_tab import ComboTab

        tab = ComboTab(profile_manager=None, demo_mode=True)
        toolbar = tab.add_combo_button.get_parent()

        assert toolbar.get_first_child() is tab.add_combo_button
        assert tab.add_combo_button.get_next_sibling() is tab.search_button

    def test_combo_tab_add_combo_requires_selected_profile(self, temp_config_dir):
        from gi.repository import Gtk

        from keymasq.gui.widgets.combo_tab import ComboTab

        tab = ComboTab(profile_manager=None, demo_mode=True)
        opened: list[str] = []
        tab._open_combo_editor = lambda combo=None: opened.append("opened")

        tab._on_add_combo_clicked(Gtk.Button())

        assert tab.section_label.get_visible() is False
        assert tab._selected_profile is None
        assert opened == []
