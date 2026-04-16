# ruff: noqa: F403, F405, I001
from tests.gui.support import *

class TestComboEditorDialog:
    def test_combo_editor_dialog_present_and_close(self):
        from gi.repository import GLib, Gtk

        from keymasq.gui.widgets.combo_editor_dialog import ComboEditorDialog

        def flush_gtk_events() -> None:
            context = GLib.MainContext.default()
            while context.iteration(False):
                pass

        parent = Gtk.Window()
        dialog = ComboEditorDialog(parent)
        closed: list[str] = []
        dialog.connect("closed", lambda *_args: closed.append("closed"))

        parent.present()
        dialog.present(parent)
        flush_gtk_events()

        assert dialog.get_visible() is True

        dialog.close()
        flush_gtk_events()

        assert closed == ["closed"]
        parent.close()

    def test_combo_editor_capture_response_adds_step(self):
        from gi.repository import Gtk

        from keymasq.gui.widgets.combo_editor_dialog import ComboEditorDialog

        parent = Gtk.Box()
        dialog = ComboEditorDialog(parent, profile_name="Desktop")

        dialog._on_capture_combo_response(
            {
                "status": "ok",
                "events": [{"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd"}],
            }
        )

        assert [event.evdev for event in dialog._draft.steps[0].events] == ["key_a"]
        assert dialog._draft.steps[0].events[0].hardware_id == "1234:5678"
        assert dialog._draft.steps[0].timeout_ms is None
        assert dialog.capture_status.get_text() == "Added step: A"

    def test_combo_editor_capture_request_uses_profile_name(self, monkeypatch):
        from gi.repository import Gtk

        import keymasq.gui.widgets.combo_editor_dialog as combo_editor_dialog_module
        from keymasq.gui.widgets.combo_editor_dialog import ComboEditorDialog

        calls: list[tuple[dict, float]] = []

        def fake_session_request_async(payload, callback, timeout=5.0):
            calls.append((payload, timeout))
            if payload.get("command") == "capture_combo":
                callback(
                    {
                        "status": "ok",
                        "events": [
                            {
                                "evdev": "key_leftctrl",
                                "hardware_id": "1234:5678",
                                "source": "kbd",
                            },
                            {"evdev": "key_s", "hardware_id": "1234:5678", "source": "kbd"},
                        ],
                    }
                )

        monkeypatch.setattr(
            combo_editor_dialog_module,
            "session_request_async",
            fake_session_request_async,
        )

        parent = Gtk.Box()
        dialog = ComboEditorDialog(parent, profile_name="Desktop")
        dialog._recording_unlocked = True
        dialog._update_capture_controls()

        dialog._on_add_step_clicked(None)

        assert calls == [
            ({"command": "get_status"}, 1.0),
            ({"command": "capture_combo", "profile_name": "Desktop", "timeout_s": 15.0}, 20.0),
        ]
        assert dialog._capture_inflight is False
        assert [event.evdev for event in dialog._draft.steps[0].events] == ["ctrl", "key_s"]
        assert dialog._draft.steps[0].timeout_ms is None

    def test_combo_editor_new_steps_after_first_default_to_600ms(self):
        from gi.repository import Gtk

        from keymasq.gui.widgets.combo_editor_dialog import ComboEditorDialog

        parent = Gtk.Box()
        dialog = ComboEditorDialog(parent, profile_name="Desktop")

        dialog._on_capture_combo_response(
            {
                "status": "ok",
                "events": [{"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd"}],
            }
        )
        dialog._on_capture_combo_response(
            {
                "status": "ok",
                "events": [{"evdev": "key_b", "hardware_id": "1234:5678", "source": "kbd"}],
            }
        )

        assert dialog._draft.steps[0].timeout_ms is None
        assert dialog._draft.steps[1].timeout_ms == 600

    def test_combo_editor_save_disabled_until_complete(self):
        from gi.repository import Gtk

        from keymasq.common.models import ActionType, ComboEvent, ComboStep, MappingAction
        from keymasq.gui.widgets.combo_editor_dialog import ComboEditorDialog

        parent = Gtk.Box()
        dialog = ComboEditorDialog(parent)

        assert dialog.save_button.get_sensitive() is False

        dialog._draft.steps.append(
            ComboStep(events=[ComboEvent(evdev="key_a", hardware_id="1234:5678")])
        )
        dialog._refresh_trigger_display()
        dialog._update_save_button()

        assert dialog.save_button.get_sensitive() is False

        dialog._on_action_selected(
            None,
            MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
        )

        assert dialog.save_button.get_sensitive() is True

    def test_combo_editor_emits_saved_combo(self):
        from gi.repository import Gtk

        from keymasq.common.models import ActionType, ComboEvent, ComboStep, MappingAction
        from keymasq.gui.widgets.combo_editor_dialog import ComboEditorDialog

        parent = Gtk.Box()
        dialog = ComboEditorDialog(parent)
        captured = []
        dialog.connect("combo-saved", lambda _dialog, combo: captured.append(combo))

        dialog.name_entry.set_text("Quick Save")
        dialog._draft.steps.append(
            ComboStep(
                events=[
                    ComboEvent(evdev="key_leftctrl", hardware_id="1234:5678", source="kbd"),
                    ComboEvent(evdev="key_s", hardware_id="1234:5678", source="kbd"),
                ]
            )
        )
        dialog._refresh_trigger_display()
        dialog._on_action_selected(
            None,
            MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
        )
        dialog._on_save_clicked(None)

        assert len(captured) == 1
        assert captured[0].name == "Quick Save"
        assert [event.evdev for event in captured[0].steps[0].events] == [
            "key_leftctrl",
            "key_s",
        ]

    def test_combo_editor_generates_default_name_when_name_is_empty(self):
        from gi.repository import Gtk

        from keymasq.common.models import ActionType, ComboEvent, ComboStep, MappingAction
        from keymasq.gui.widgets.combo_editor_dialog import ComboEditorDialog

        parent = Gtk.Box()
        dialog = ComboEditorDialog(parent)
        captured = []
        dialog.connect("combo-saved", lambda _dialog, combo: captured.append(combo))

        dialog._draft.steps.append(
            ComboStep(
                events=[
                    ComboEvent(evdev="key_leftctrl", hardware_id="1234:5678", source="kbd"),
                    ComboEvent(evdev="key_s", hardware_id="1234:5678", source="kbd"),
                ]
            )
        )
        dialog._refresh_trigger_display()
        dialog._on_action_selected(
            None,
            MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
        )
        dialog._on_save_clicked(None)

        assert len(captured) == 1
        assert captured[0].name == "Ctrl+S -> F5"

    def test_combo_editor_step_timeout_controls_and_save(self):
        from gi.repository import Gtk

        from keymasq.common.models import (
            ActionType,
            ComboConfig,
            ComboEvent,
            ComboStep,
            MappingAction,
        )
        from keymasq.gui.widgets.combo_editor_dialog import ComboEditorDialog

        def child_widgets(widget):
            children = []
            child = widget.get_first_child()
            while child is not None:
                children.append(child)
                child = child.get_next_sibling()
            return children

        parent = Gtk.Box()
        dialog = ComboEditorDialog(
            parent,
            ComboConfig(
                id="combo-1",
                name="Quick Save",
                steps=[
                    ComboStep(events=[ComboEvent(evdev="key_a", hardware_id="1234:5678")]),
                    ComboStep(
                        events=[ComboEvent(evdev="key_b", hardware_id="1234:5678")],
                        timeout_ms=700,
                    ),
                ],
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
            ),
        )

        first_row = dialog.steps_box.get_first_child()
        second_row = first_row.get_next_sibling()

        assert not any(isinstance(child, Gtk.SpinButton) for child in child_widgets(first_row))
        second_spins = [
            child for child in child_widgets(second_row) if isinstance(child, Gtk.SpinButton)
        ]
        assert len(second_spins) == 1
        second_spins[0].set_value(850)

        assert dialog._draft.steps[0].timeout_ms is None
        assert dialog._draft.steps[1].timeout_ms == 850

    def test_combo_editor_trigger_recall_and_restore_controls(self):
        from gi.repository import Gtk

        from keymasq.common.models import ActionType, ComboEvent, ComboStep, MappingAction
        from keymasq.gui.widgets.combo_editor_dialog import ComboEditorDialog

        parent = Gtk.Box()
        dialog = ComboEditorDialog(parent)
        dialog._draft.steps.append(
            ComboStep(
                events=[
                    ComboEvent(evdev="key_leftctrl", hardware_id="1234:5678", source="kbd"),
                    ComboEvent(evdev="key_x", hardware_id="1234:5678", source="kbd"),
                ]
            )
        )
        dialog._normalize_restore_trigger_keys()
        dialog._refresh_trigger_display()

        assert dialog.recall_trigger_keys_row.get_active() is False
        assert dialog.restore_trigger_keys_group.get_visible() is False
        assert dialog._restore_trigger_key_rows == []

        dialog.recall_trigger_keys_row.set_active(True)
        assert dialog.restore_trigger_keys_group.get_visible() is True
        labels = [
            dialog._restore_trigger_key_labels[key].get_text() for key in ("ctrl", "key_x")
        ]
        assert labels == ["Ctrl", "X"]
        dialog._restore_trigger_key_buttons["ctrl"].set_active(True)
        dialog._on_action_selected(
            None,
            MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
        )

        assert dialog._draft.recall_trigger_keys is True
        assert dialog._draft.restore_trigger_keys == ["ctrl"]
        assert dialog.save_button.get_sensitive() is True

    def test_combo_editor_exact_duplicate_is_rejected(self):
        from gi.repository import Gtk

        from keymasq.common.models import (
            ActionType,
            ComboConfig,
            ComboEvent,
            ComboStep,
            MappingAction,
        )
        from keymasq.gui.widgets.combo_editor_dialog import ComboEditorDialog

        parent = Gtk.Box()
        dialog = ComboEditorDialog(
            parent,
            ComboConfig(
                id="combo-2",
                name="Long Combo",
                steps=[
                    ComboStep(
                        events=[
                            ComboEvent(evdev="key_leftctrl", hardware_id="1234:5678", source="kbd"),
                            ComboEvent(evdev="key_x", hardware_id="1234:5678", source="kbd"),
                        ]
                    ),
                    ComboStep(
                        events=[ComboEvent(evdev="key_1", hardware_id="1234:5678", source="kbd")],
                        timeout_ms=600,
                    ),
                ],
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
            ),
            sibling_combos=[
                ComboConfig(
                    id="combo-1",
                    name="Duplicate Combo",
                    steps=[
                        ComboStep(
                            events=[
                                ComboEvent(
                                    evdev="key_leftctrl",
                                    hardware_id="1234:5678",
                                    source="kbd",
                                ),
                                ComboEvent(evdev="key_x", hardware_id="1234:5678", source="kbd"),
                            ]
                        ),
                        ComboStep(
                            events=[
                                ComboEvent(evdev="key_1", hardware_id="1234:5678", source="kbd")
                            ]
                        ),
                    ],
                    action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f6"),
                )
            ],
        )

        assert dialog.validation_label.get_visible() is True
        assert "same trigger already exists" in dialog.validation_label.get_text().lower()
        assert dialog.save_button.get_sensitive() is False

    def test_combo_editor_prefix_shadow_does_not_block_save(self):
        from gi.repository import Gtk

        from keymasq.common.models import (
            ActionType,
            ComboConfig,
            ComboEvent,
            ComboStep,
            MappingAction,
        )
        from keymasq.gui.widgets.combo_editor_dialog import ComboEditorDialog

        parent = Gtk.Box()
        dialog = ComboEditorDialog(
            parent,
            ComboConfig(
                id="combo-2",
                name="Long Combo",
                steps=[
                    ComboStep(
                        events=[
                            ComboEvent(evdev="key_leftctrl", hardware_id="1234:5678", source="kbd"),
                            ComboEvent(evdev="key_x", hardware_id="1234:5678", source="kbd"),
                        ]
                    ),
                    ComboStep(
                        events=[ComboEvent(evdev="key_1", hardware_id="1234:5678", source="kbd")],
                        timeout_ms=600,
                    ),
                ],
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
            ),
            sibling_combos=[
                ComboConfig(
                    id="combo-1",
                    name="Short Combo",
                    steps=[
                        ComboStep(
                            events=[
                                ComboEvent(
                                    evdev="key_leftctrl",
                                    hardware_id="1234:5678",
                                    source="kbd",
                                ),
                                ComboEvent(evdev="key_x", hardware_id="1234:5678", source="kbd"),
                            ]
                        )
                    ],
                    action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f6"),
                )
            ],
        )

        assert dialog.save_button.get_sensitive() is True

    def test_combo_editor_allows_saved_superkey_actions(self, temp_config_dir, monkeypatch):
        from gi.repository import Gtk

        from keymasq.common import paths
        from keymasq.common.models import (
            ActionType,
            ComboEvent,
            ComboStep,
            MappingAction,
            SuperkeyAction,
            SuperkeyConfig,
            SuperkeyMode,
        )
        from keymasq.gui.widgets.combo_editor_dialog import ComboEditorDialog
        from keymasq.session.superkeys import SuperkeyManager

        superkeys_dir = temp_config_dir / "superkeys"
        superkeys_dir.mkdir()
        monkeypatch.setattr(paths, "SUPERKEYS_DIR", superkeys_dir)
        SuperkeyManager().save_superkey(
            SuperkeyConfig(
                name="combo_overload",
                mode=SuperkeyMode.OVERLOAD,
                overload_actions=[
                    MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
                ],
            )
        )
        SuperkeyManager().save_superkey(
            SuperkeyConfig(
                name="combo_pattern",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[SuperkeyAction(action_type=ActionType.KEYBOARD, target="key_b")],
                double_tap_actions=[
                    SuperkeyAction(action_type=ActionType.KEYBOARD, target="key_c")
                ],
                tap_hold_actions=[
                    SuperkeyAction(action_type=ActionType.KEYBOARD, target="key_d")
                ],
            )
        )

        parent = Gtk.Box()

        overload_dialog = ComboEditorDialog(parent)
        overload_dialog._draft.steps.append(
            ComboStep(events=[ComboEvent(evdev="key_a", hardware_id="1234:5678")])
        )
        overload_dialog._refresh_trigger_display()
        overload_dialog._on_action_selected(
            None,
            MappingAction(action_type=ActionType.SUPERKEY, superkey_name="combo_overload"),
        )

        assert overload_dialog.validation_label.get_visible() is False
        assert overload_dialog.save_button.get_sensitive() is True

        pattern_dialog = ComboEditorDialog(parent)
        pattern_dialog._draft.steps.extend(
            [
                ComboStep(events=[ComboEvent(evdev="key_x", hardware_id="1234:5678")]),
                ComboStep(events=[ComboEvent(evdev="key_y", hardware_id="1234:5678")]),
            ]
        )
        pattern_dialog._refresh_trigger_display()
        pattern_dialog._on_action_selected(
            None,
            MappingAction(action_type=ActionType.SUPERKEY, superkey_name="combo_pattern"),
        )

        assert pattern_dialog.validation_label.get_visible() is False
        assert pattern_dialog.save_button.get_sensitive() is True

    def test_combo_editor_rejects_missing_superkey_action(self):
        from gi.repository import Gtk

        from keymasq.common.models import ActionType, ComboEvent, ComboStep, MappingAction
        from keymasq.gui.widgets.combo_editor_dialog import ComboEditorDialog

        parent = Gtk.Box()
        dialog = ComboEditorDialog(parent)
        dialog._draft.steps.append(
            ComboStep(events=[ComboEvent(evdev="key_a", hardware_id="1234:5678")])
        )
        dialog._refresh_trigger_display()
        dialog._on_action_selected(
            None,
            MappingAction(action_type=ActionType.SUPERKEY, superkey_name="missing-superkey"),
        )

        assert dialog.validation_label.get_visible() is True
        assert "could not be loaded" in dialog.validation_label.get_text().lower()
        assert dialog.save_button.get_sensitive() is False


