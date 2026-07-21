import pytest

pytest.importorskip("gi")


class TestProfileCreateDialog:
    def test_new_profile_defaults_to_permanent(self, temp_config_dir):
        from keymasq.common.model.profiles import ProfileConfig
        from keymasq.gui.wizards.profile_create import ProfileCreateDialog
        from keymasq.session.profile.manager import ProfileManager

        profile_manager = ProfileManager()
        profile_manager.save_profile(
            ProfileConfig(
                name="Base",
                enabled=True,
                is_permanent=True,
                priority=4,
            )
        )
        dialog = ProfileCreateDialog(None, profile_manager)
        dialog.name_entry.set_text("Gaming")
        dialog._on_create(None)

        created = profile_manager.get_profile("Gaming")

        assert created is not None
        assert created.config.is_permanent is True
        assert created.config.priority == 5


class TestProfileManagedTab:
    def test_window_rules_summary_wraps_all_rules_in_action_row_subtitle(self):
        from pathlib import Path

        from gi.repository import Gtk

        from keymasq.common.model.profiles import ProfileConfig, WindowRule
        from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab
        from keymasq.session.profile.types import ProfileInfo

        class ProfileManagerStub:
            def list_profiles(self):
                return []

        tab = ProfileManagedTab(ProfileManagerStub(), demo_mode=True)
        tab.settings_btn = Gtk.Button()
        tab._setup_profile_settings()
        tab._selected_profile = ProfileInfo(
            Path("browser.toml"),
            ProfileConfig(
                name="browser",
                window_rules=[
                    WindowRule(field="class", pattern="brave\\-origin|librewolf"),
                    WindowRule(
                        field="title",
                        pattern="Subscriptions\\ \\-\\ YouTube\\ \\-\\ Brave\\ Origin",
                    ),
                    WindowRule(field="tag", pattern="browser"),
                ],
            ),
        )

        tab._update_rules_label()

        assert tab.window_rules_row.get_subtitle_lines() == 0
        assert tab.window_rules_row.get_subtitle() == (
            "class=brave\\-origin|librewolf\n"
            "title=Subscriptions\\ \\-\\ YouTube\\ \\-\\ Brave\\ Origin\n"
            "tag=browser - conditional"
        )

    def test_window_rule_capture_forwards_timeout_and_restores_ui(self, monkeypatch):
        from keymasq.gui.widgets import profile_managed_tab as profile_managed_tab_module
        from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab

        class ProfileManagerStub:
            def list_profiles(self):
                return []

        class LabelStub:
            def __init__(self):
                self.text = ""

            def set_text(self, text):
                self.text = text

        class ButtonStub:
            def __init__(self):
                self.sensitive = False

            def set_sensitive(self, sensitive):
                self.sensitive = sensitive

        requests = []
        timeout_not_provided = object()

        def session_request_async(payload, callback, timeout=timeout_not_provided):
            requests.append((payload, timeout))
            callback({"status": "error", "message": "Capture unavailable"})

        monkeypatch.setattr(
            profile_managed_tab_module,
            "session_request_async",
            session_request_async,
        )

        tab = ProfileManagedTab(ProfileManagerStub())
        tab._window_rule_capture_pending = True
        tab._window_rule_capture_generation = 1
        tab._window_rule_capture_status = LabelStub()
        tab._window_rule_capture_btn = ButtonStub()

        assert tab._capture_window_rules_after_delay() is False
        assert requests == [({"command": "get_active_window"}, 5.0)]
        assert tab._window_rule_capture_pending is False
        assert tab._window_rule_capture_btn.sensitive is True
        assert tab._window_rule_capture_status.text == "Capture unavailable"

    def test_lifecycle_macro_dropdown_reloads_on_macro_saved_event(self, monkeypatch):
        from keymasq.gui.widgets import profile_managed_tab as profile_managed_tab_module
        from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab

        class ProfileManagerStub:
            def list_profiles(self):
                return []

        class Parent:
            def __init__(self):
                self.handlers = {}
                self._selected_profile_name = None

            def register_event_handler(self, event_type, callback):
                self.handlers.setdefault(event_type, []).append(callback)

            def unregister_event_handler(self, event_type, callback):
                self.handlers[event_type].remove(callback)

        responses = [
            {"status": "ok", "macros": []},
            {"status": "ok", "macros": [{"name": "new_macro"}]},
        ]
        requests = []

        def session_request_async(payload, callback, timeout=5.0):
            _ = timeout
            requests.append(payload)
            callback(responses.pop(0))

        monkeypatch.setattr(
            profile_managed_tab_module,
            "session_request_async",
            session_request_async,
        )

        parent = Parent()
        tab = ProfileManagedTab(ProfileManagerStub(), main_window=parent)
        tab._setup_profile_selector()

        parent.handlers["macro_saved"][0]({"event": "macro_saved", "name": "new_macro"})

        assert requests == [{"command": "list_macros"}, {"command": "list_macros"}]
        assert tab._profile_lifecycle_macro_options == ["", "new_macro"]

    def test_rename_updates_copy_name_collision_cache(self, monkeypatch):
        from pathlib import Path

        from keymasq.common.model.profiles import ProfileConfig
        from keymasq.gui.widgets import profile_managed_tab as profile_managed_tab_module
        from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab
        from keymasq.session.profile.types import ProfileInfo

        old_profile = ProfileInfo(Path("old.toml"), ProfileConfig(name="Old"))
        other_profile = ProfileInfo(Path("project.toml"), ProfileConfig(name="Project"))
        renamed_profile = ProfileInfo(
            Path("project_1.toml"),
            ProfileConfig(name="Project_1"),
        )

        class ProfileManagerStub:
            def __init__(self):
                self.profiles = [old_profile, other_profile]

            def list_profiles(self):
                return list(self.profiles)

            def rename_profile(self, old_name, new_name):
                assert old_name == "Old"
                assert new_name == "Project_1"
                self.profiles[0] = renamed_profile
                return renamed_profile

            def get_next_priority(self):
                return 1

        class EntryStub:
            def get_text(self):
                return "Project_1"

        monkeypatch.setattr(
            profile_managed_tab_module,
            "notify_session_reload_async",
            lambda: None,
        )

        tab = ProfileManagedTab(ProfileManagerStub())
        tab._selected_profile = old_profile
        tab._profile_names = ["__passthrough__", "Old", "Project"]
        tab._profile_items = [None, old_profile, other_profile]
        tab._refresh_profile_dropdown_states = lambda: None
        tab._refresh_other_profile_tabs = lambda preferred_profile_name=None: None

        tab._on_name_changed(EntryStub())
        tab._selected_profile = other_profile

        copy_config = tab._build_profile_copy_config()

        assert [profile.config.name for profile in tab.profiles] == ["Project_1", "Project"]
        assert tab._profile_items[1] is renamed_profile
        assert copy_config is not None
        assert copy_config.name == "Project_2"

    def test_delete_last_profile_is_blocked(self, monkeypatch):
        from pathlib import Path

        from keymasq.common.model.profiles import ProfileConfig
        from keymasq.gui.widgets import profile_managed_tab as profile_managed_tab_module
        from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab
        from keymasq.session.profile.types import ProfileInfo

        only_profile = ProfileInfo(Path("base.toml"), ProfileConfig(name="Base"))

        class ProfileManagerStub:
            def __init__(self):
                self.deleted: list[str] = []

            def list_profiles(self):
                return [only_profile]

            def delete_profile(self, name: str):
                self.deleted.append(name)
                return True

        class DialogStub:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        manager = ProfileManagerStub()
        reloads: list[bool] = []
        monkeypatch.setattr(
            profile_managed_tab_module,
            "notify_session_reload_async",
            lambda: reloads.append(True),
        )

        tab = ProfileManagedTab(manager)
        tab._selected_profile = only_profile
        errors: list[str] = []
        tab._show_profile_error_dialog = errors.append
        dialog = DialogStub()

        tab._on_confirm_delete_profile(None, dialog)

        assert dialog.closed is True
        assert manager.deleted == []
        assert reloads == []
        assert errors == [
            "At least one profile is required. Create another profile before deleting this one."
        ]


class TestProfileActions:
    def test_action_types(self):
        from keymasq.common.model.core import ActionType

        assert ActionType.PASSTHROUGH.value == "passthrough"
        assert ActionType.KEYBOARD.value == "keyboard"
        assert ActionType.MOUSE.value == "mouse"
        assert ActionType.EXEC.value == "exec"
        assert ActionType.COMPOSITOR_DISPATCH.value == "compositor_dispatch"
        assert ActionType.SUPPRESS.value == "suppress"
        assert ActionType.REPEAT.value == "repeat"
