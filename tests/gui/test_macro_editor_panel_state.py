import pytest

pytest.importorskip("gi")

from keymasq.gui.widgets.macro_editor.model import EditableControl
from keymasq.gui.widgets.macro_editor.panel.controls import (
    control_editor_state,
    timeout_policy_hint,
)
from keymasq.gui.widgets.macro_editor.panel.settings import loop_control_state


@pytest.mark.parametrize(
    ("mode", "show_count", "show_stop_behavior"),
    [
        ("none", False, False),
        ("count", True, False),
        ("hold", False, True),
        ("toggle", False, True),
        ("unknown", False, False),
    ],
)
def test_loop_control_state_is_independent_of_widgets(
    mode: str,
    show_count: bool,
    show_stop_behavior: bool,
) -> None:
    state = loop_control_state(mode)

    assert state.show_count is show_count
    assert state.show_stop_behavior is show_stop_behavior


def test_control_editor_state_resolves_wait_ranges() -> None:
    wait = control_editor_state(
        EditableControl(mode="wait", t_us=0, duration_us=125_000),
        30_000,
    )
    random_wait = control_editor_state(
        EditableControl(
            mode="wait_random",
            t_us=0,
            min_us=20_000,
            max_us=80_000,
        ),
        30_000,
    )

    assert wait.show_ab is True
    assert wait.a_label == "Duration (ms):"
    assert wait.a_value_ms == 125.0
    assert wait.show_a is True
    assert wait.show_b is False
    assert random_wait.a_label == "Min (ms):"
    assert random_wait.a_value_ms == 20.0
    assert random_wait.b_label == "Max (ms):"
    assert random_wait.b_value_ms == 80.0
    assert random_wait.show_b is True


def test_control_editor_state_resolves_command_policy() -> None:
    async_command = control_editor_state(
        EditableControl(mode="exec_async", t_us=0, command="notify-send done"),
        30_000,
    )
    sync_command = control_editor_state(
        EditableControl(
            mode="exec_sync",
            t_us=0,
            command="sleep 1",
            timeout_ms=45_000,
            inhibit_mouse=True,
        ),
        30_000,
    )
    parallel_command = control_editor_state(
        EditableControl(
            mode="exec_parallel",
            t_us=0,
            command="sleep 2",
            timeout_ms=15_000,
            inhibit_mouse=True,
        ),
        30_000,
    )

    assert async_command.show_command is True
    assert async_command.command == "notify-send done"
    assert async_command.title == "Run Command"
    assert async_command.detail == "Run detached"
    assert async_command.show_exec_mode is True
    assert async_command.exec_mode == "exec_async"
    assert async_command.show_sync is False
    assert sync_command.show_command is True
    assert sync_command.title == "Run Command"
    assert sync_command.detail == "Wait for completion"
    assert sync_command.show_exec_mode is True
    assert sync_command.exec_mode == "exec_sync"
    assert sync_command.show_sync is True
    assert sync_command.timeout_ms == 45_000
    assert sync_command.inhibit_mouse is True
    assert sync_command.timeout_hint == "Runtime clamp: 45000ms -> 30000ms"
    assert parallel_command.detail == "Run in parallel"
    assert parallel_command.exec_mode == "exec_parallel"
    assert parallel_command.show_sync is True
    assert parallel_command.timeout_ms == 15_000
    assert parallel_command.inhibit_mouse is True
    assert timeout_policy_hint(1_000, 30_000) == "Policy max timeout: 30000ms"


def test_control_editor_state_resolves_compositor_action() -> None:
    state = control_editor_state(
        EditableControl(
            mode="compositor_dispatch",
            t_us=0,
            compositor_id="hyprland",
            compositor_dispatcher="workspace",
            compositor_args="2",
        ),
        30_000,
    )

    assert state.title == "Compositor Action"
    assert state.show_change is True
    assert state.change_label == "Change Action..."
    assert "workspace" in state.detail


def test_control_editor_state_resolves_macro_call() -> None:
    state = control_editor_state(
        EditableControl(
            mode="macro_parallel",
            t_us=0,
            macro_name="child",
            macro_speed=1.5,
            macro_loop_mode="hold",
            macro_loop_stop_behavior="cancel_run",
            macro_replay_mouse_movement=False,
        ),
        30_000,
    )

    assert state.title == "Macro Call"
    assert state.title_context == "child"
    assert state.detail == "Run in parallel, while held"
    assert state.show_change is True
    assert state.change_label == "Change Macro..."
    assert state.show_macro is True
    assert state.macro_wait is False
    assert state.macro_loop_mode == "hold"
    assert state.macro_loop_stop_behavior == "cancel_run"
    assert state.macro_speed == 1.5
    assert state.macro_replay_mouse_movement is False
