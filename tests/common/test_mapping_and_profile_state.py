# ruff: noqa: F403, F405, I001
from tests.common.support import *

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
