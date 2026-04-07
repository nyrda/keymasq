from __future__ import annotations

from keyforge.common.models import ActionType, MappingAction
from keyforge.gui.widgets.compositor_actions.core import (
    CompositorActionDefinition,
    CompositorActionPreset,
)

NIRI_DISPATCH_PRESETS = (
    CompositorActionPreset(
        label="Close Window",
        dispatcher="close_window",
        args="",
        hint="Close the focused window.",
    ),
    CompositorActionPreset(
        label="Fullscreen",
        dispatcher="fullscreen_toggle",
        args="",
        hint="Toggle fullscreen on the focused window.",
    ),
    CompositorActionPreset(
        label="Fake Fullscreen",
        dispatcher="windowed_fullscreen_toggle",
        args="",
        hint="Toggle windowed fullscreen on the focused window.",
    ),
    CompositorActionPreset(
        label="Toggle Floating",
        dispatcher="toggle_window_floating",
        args="",
        hint="Toggle floating mode for the focused window.",
    ),
    CompositorActionPreset(
        label="Center Window",
        dispatcher="center_window",
        args="",
        hint="Center the focused floating window.",
    ),
    CompositorActionPreset(
        label="Previous Window",
        dispatcher="focus_previous_window",
        args="",
        hint="Focus the previous window in Niri's scrolling order, looping to the end.",
    ),
    CompositorActionPreset(
        label="Next Window",
        dispatcher="focus_next_window",
        args="",
        hint="Focus the next window in Niri's scrolling order, looping to the start.",
    ),
    CompositorActionPreset(
        label="Focus Left",
        dispatcher="focus_column_left",
        args="",
        hint="Focus the column to the left.",
    ),
    CompositorActionPreset(
        label="Focus Right",
        dispatcher="focus_column_right",
        args="",
        hint="Focus the column to the right.",
    ),
    CompositorActionPreset(
        label="Focus Up",
        dispatcher="focus_window_up",
        args="",
        hint="Focus the window above.",
    ),
    CompositorActionPreset(
        label="Focus Down",
        dispatcher="focus_window_down",
        args="",
        hint="Focus the window below.",
    ),
    CompositorActionPreset(
        label="Move Column Left",
        dispatcher="move_column_left",
        args="",
        hint="Move the focused column to the left.",
    ),
    CompositorActionPreset(
        label="Move Column Right",
        dispatcher="move_column_right",
        args="",
        hint="Move the focused column to the right.",
    ),
    CompositorActionPreset(
        label="Move Window Up",
        dispatcher="move_window_up",
        args="",
        hint="Move the focused window up in its column.",
    ),
    CompositorActionPreset(
        label="Move Window Down",
        dispatcher="move_window_down",
        args="",
        hint="Move the focused window down in its column.",
    ),
    CompositorActionPreset(
        label="Workspace Up",
        dispatcher="focus_workspace_up",
        args="",
        hint="Focus the workspace above.",
    ),
    CompositorActionPreset(
        label="Workspace Down",
        dispatcher="focus_workspace_down",
        args="",
        hint="Focus the workspace below.",
    ),
    CompositorActionPreset(
        label="Workspace Previous",
        dispatcher="focus_workspace_previous",
        args="",
        hint="Focus the previously focused workspace.",
    ),
    CompositorActionPreset(
        label="Workspace 1",
        dispatcher="focus_workspace",
        args="1",
        hint="Focus workspace 1.",
    ),
    CompositorActionPreset(
        label="Workspace 2",
        dispatcher="focus_workspace",
        args="2",
        hint="Focus workspace 2.",
    ),
    CompositorActionPreset(
        label="Move To Workspace 1",
        dispatcher="move_window_to_workspace",
        args="1",
        hint="Move the focused window to workspace 1 and follow it.",
    ),
    CompositorActionPreset(
        label="Move To Workspace 2",
        dispatcher="move_window_to_workspace",
        args="2",
        hint="Move the focused window to workspace 2 and follow it.",
    ),
    CompositorActionPreset(
        label="Send To Workspace 1",
        dispatcher="send_window_to_workspace",
        args="1",
        hint="Send the focused window to workspace 1 without following it.",
    ),
    CompositorActionPreset(
        label="Send To Workspace 2",
        dispatcher="send_window_to_workspace",
        args="2",
        hint="Send the focused window to workspace 2 without following it.",
    ),
)


def _niri_available(current_action: MappingAction | None, status: dict[str, object]) -> bool:
    _ = current_action
    return bool(
        status.get("listener_name") == "niri"
        and status.get("compositor_dispatch_available") is True
    )


def _niri_fields(current_action: MappingAction | None) -> tuple[str, str]:
    if current_action is None or current_action.action_type != ActionType.COMPOSITOR_DISPATCH:
        return "", ""
    compositor_id = str(current_action.compositor_id or "").strip()
    if compositor_id and compositor_id != "niri":
        return "", ""
    return (
        str(current_action.compositor_dispatcher or ""),
        str(current_action.compositor_args or ""),
    )


def _build_niri_action(dispatcher: str, args: str) -> MappingAction:
    return MappingAction(
        action_type=ActionType.COMPOSITOR_DISPATCH,
        compositor_id="niri",
        compositor_dispatcher=dispatcher,
        compositor_args=args,
    )


def _describe_niri_action(action: MappingAction) -> str:
    args = str(action.compositor_args or "").strip()
    suffix = f" {args}" if args else ""
    return f"Niri → {action.compositor_dispatcher or '?'}{suffix}"


NIRI_ACTION_DEFINITION = CompositorActionDefinition(
    page_id="niri",
    compositor_id="niri",
    title="Niri",
    subtitle="Send a supported Niri action through the active Niri listener.",
    dispatcher_placeholder="e.g. focus_workspace",
    args_placeholder="e.g. 2 or name:web",
    action_type=ActionType.COMPOSITOR_DISPATCH,
    presets=NIRI_DISPATCH_PRESETS,
    allow_custom=False,
    is_available=_niri_available,
    extract_fields=_niri_fields,
    build_action=_build_niri_action,
    describe_action=_describe_niri_action,
)
