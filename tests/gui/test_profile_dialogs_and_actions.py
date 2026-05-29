# ruff: noqa: F403, F405, I001
from tests.gui.support import *

class TestProfileCreateDialog:
    def test_new_profile_defaults_to_permanent(self, temp_config_dir):
        from keymasq.common.models import ProfileConfig
        from keymasq.gui.wizards.profile_create import ProfileCreateDialog
        from keymasq.session.profiles import ProfileManager

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

        from keymasq.common.models import ProfileConfig
        from keymasq.gui.widgets import profile_managed_tab as profile_managed_tab_module
        from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab
        from keymasq.session.profiles import ProfileInfo

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


class TestProfileActions:
    def test_action_types(self):
        from keymasq.common.models import ActionType

        assert ActionType.PASSTHROUGH.value == "passthrough"
        assert ActionType.KEYBOARD.value == "keyboard"
        assert ActionType.MOUSE.value == "mouse"
        assert ActionType.EXEC.value == "exec"
        assert ActionType.COMPOSITOR_DISPATCH.value == "compositor_dispatch"
        assert ActionType.SUPPRESS.value == "suppress"
        assert ActionType.REPEAT.value == "repeat"
