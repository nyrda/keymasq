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


class TestApplication:
    def test_application_args(self):
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--demo", action="store_true")
        parser.add_argument("--version", action="version", version="test")

        args = parser.parse_args(["--demo"])
        assert args.demo is True

        args = parser.parse_args([])
        assert args.demo is False


class TestButtonWidget:
    def test_button_widget_creation(self):
        from keymasq.common.models import ButtonDefinition

        button = ButtonDefinition(
            id="btn_left",
            label="Left Click",
            evdev="btn_left",
            zone="left",
        )

        assert button.id == "btn_left"
        assert button.label == "Left Click"
        assert button.evdev == "btn_left"
        assert button.zone == "left"


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


class TestProfileActions:
    def test_action_types(self):
        from keymasq.common.models import ActionType

        assert ActionType.PASSTHROUGH.value == "passthrough"
        assert ActionType.KEYBOARD.value == "keyboard"
        assert ActionType.MOUSE.value == "mouse"
        assert ActionType.EXEC.value == "exec"
        assert ActionType.COMPOSITOR_DISPATCH.value == "compositor_dispatch"
        assert ActionType.SUPPRESS.value == "suppress"

    def test_mapping_action_keyboard(self):
        from keymasq.common.models import ActionType, MappingAction

        action = MappingAction(
            action_type=ActionType.KEYBOARD,
            target="key_space",
        )

        assert action.action_type == ActionType.KEYBOARD
        assert action.target == "key_space"

    def test_mapping_action_with_rapidfire(self):
        from keymasq.common.models import ActionType, MappingAction

        action = MappingAction(
            action_type=ActionType.KEYBOARD,
            target="btn_left",
            rapidfire_enabled=True,
            rapidfire_hold_ms=50,
            rapidfire_wait_ms=30,
        )

        assert action.action_type == ActionType.KEYBOARD
        assert action.rapidfire_enabled is True
        assert action.rapidfire_hold_ms == 50
        assert action.rapidfire_wait_ms == 30
