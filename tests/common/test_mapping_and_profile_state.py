import pytest

from keymasq.common.models import (
    ActionType,
    MappingAction,
    ProfileConfig,
    ProfileState,
    SuperkeyAction,
    WindowRule,
    is_protected_button,
    mapping_action_to_superkey_action,
    superkey_action_to_mapping_action,
)


class TestMappingAction:
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

    def test_rapidfire_clamps_to_fastest_supported_pattern(self):
        action = MappingAction(
            action_type=ActionType.KEYBOARD,
            target="key_a",
            rapidfire_enabled=True,
            rapidfire_hold_ms=-5,
            rapidfire_wait_ms=0,
        )

        assert action.rapidfire_enabled is True
        assert action.rapidfire_hold_ms == 0
        assert action.rapidfire_wait_ms == 1

    def test_gamepad_axis_normalizes_output_and_clamps_value(self):
        action = MappingAction(
            action_type=ActionType.GAMEPAD_AXIS,
            target="x",
            output_id="virtual-gamepad-2",
            axis_value=-40000,
            rapidfire_enabled=True,
        )

        assert action.target == "abs_x"
        assert action.output_id == "virtual-gamepad-2"
        assert action.axis_value == -32768
        assert action.rapidfire_enabled is True

    def test_gamepad_axis_invalid_target_neutralizes_value(self):
        mapping_action = MappingAction(
            action_type=ActionType.GAMEPAD_AXIS,
            target="btn_lt",
            axis_value=255,
        )
        superkey_action = SuperkeyAction(
            action_type=ActionType.GAMEPAD_AXIS,
            target="btn_rt",
            axis_value=255,
        )

        assert mapping_action.target is None
        assert mapping_action.axis_value == 0
        assert superkey_action.target is None
        assert superkey_action.axis_value == 0

    def test_gamepad_axis_superkey_conversion_preserves_value(self):
        action = MappingAction(
            action_type=ActionType.GAMEPAD_AXIS,
            target="abs_rx",
            axis_value=12345,
            output_id="virtual-gamepad-2",
        )

        superkey_action = mapping_action_to_superkey_action(action)
        roundtrip = superkey_action_to_mapping_action(superkey_action)

        assert superkey_action.axis_value == 12345
        assert roundtrip.axis_value == 12345
        assert roundtrip.output_id == "virtual-gamepad-2"

    def test_repeat_action_normalizes_categories_and_supports_rapidfire(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        action = MappingAction(
            action_type=ActionType.REPEAT,
            repeat_categories=[
                "keyboard",
                "invalid",
                "mouse_button",
                "mouse_wheel",
                "mouse",
                "keyboard",
            ],
            rapidfire_enabled=True,
            rapidfire_hold_ms=-1,
            rapidfire_wait_ms=0,
        )
        default_action = MappingAction(action_type=ActionType.REPEAT)
        with caplog.at_level("WARNING", logger="keymasq.common.models"):
            invalid_action = MappingAction(
                action_type=ActionType.REPEAT,
                repeat_categories=["invalid"],
            )
        empty_action = MappingAction(
            action_type=ActionType.REPEAT,
            repeat_categories=[],
        )
        malformed_action = MappingAction(
            action_type=ActionType.REPEAT,
            repeat_categories=42,  # pyright: ignore[reportArgumentType]
        )
        unrelated = MappingAction(
            action_type=ActionType.KEYBOARD,
            target="key_a",
            repeat_categories=["keyboard"],
        )

        assert action.repeat_categories == ["keyboard", "mouse"]
        assert action.rapidfire_enabled is True
        assert action.rapidfire_hold_ms == 0
        assert action.rapidfire_wait_ms == 1
        assert default_action.repeat_categories == [
            "keyboard",
            "mouse",
            "gamepad",
            "macro",
            "special",
        ]
        assert invalid_action.repeat_categories == [
            "keyboard",
            "mouse",
            "gamepad",
            "macro",
            "special",
        ]
        assert "Invalid repeat_categories" in caplog.text
        assert empty_action.repeat_categories == []
        assert malformed_action.repeat_categories == [
            "keyboard",
            "mouse",
            "gamepad",
            "macro",
            "special",
        ]
        assert unrelated.repeat_categories is None


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
