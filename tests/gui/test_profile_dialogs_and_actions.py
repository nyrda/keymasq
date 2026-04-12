# ruff: noqa: F403, F405, I001
from tests.gui.support import *

class TestProfileCreateDialog:
    def test_new_profile_defaults_to_permanent(self, temp_config_dir):
        from keyforge.common.models import ProfileConfig
        from keyforge.gui.wizards.profile_create import ProfileCreateDialog
        from keyforge.session.profiles import ProfileManager

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
        from keyforge.common.models import ButtonDefinition

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

class TestProfileActions:
    def test_action_types(self):
        from keyforge.common.models import ActionType

        assert ActionType.PASSTHROUGH.value == "passthrough"
        assert ActionType.KEYBOARD.value == "keyboard"
        assert ActionType.MOUSE.value == "mouse"
        assert ActionType.EXEC.value == "exec"
        assert ActionType.COMPOSITOR_DISPATCH.value == "compositor_dispatch"
        assert ActionType.SUPPRESS.value == "suppress"

    def test_mapping_action_keyboard(self):
        from keyforge.common.models import ActionType, MappingAction

        action = MappingAction(
            action_type=ActionType.KEYBOARD,
            target="key_space",
        )

        assert action.action_type == ActionType.KEYBOARD
        assert action.target == "key_space"

    def test_mapping_action_with_rapidfire(self):
        from keyforge.common.models import ActionType, MappingAction

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
