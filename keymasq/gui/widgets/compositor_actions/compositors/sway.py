from __future__ import annotations

from keymasq.common.model.actions import MappingAction
from keymasq.gui.widgets.compositor_actions.core import (
    CompositorActionPreset,
    build_compositor_dispatch_definition,
)


def _preset(label: str, dispatcher: str, hint: str, args: str = "") -> CompositorActionPreset:
    return CompositorActionPreset(label=label, dispatcher=dispatcher, args=args, hint=hint)


SWAY_DISPATCH_PRESETS = (
    _preset("Close Window", "kill", "Close the focused window."),
    _preset("Fullscreen", "fullscreen toggle", "Toggle fullscreen on the focused window."),
    _preset("Toggle Floating", "floating toggle", "Toggle floating mode for the focused window."),
    _preset("Toggle Sticky", "sticky toggle", "Keep the floating window on every workspace."),
    _preset("Focus Left", "focus left", "Focus the window to the left."),
    _preset("Focus Right", "focus right", "Focus the window to the right."),
    _preset("Focus Up", "focus up", "Focus the window above."),
    _preset("Focus Down", "focus down", "Focus the window below."),
    _preset("Focus Parent", "focus parent", "Focus the parent container."),
    _preset(
        "Toggle Tiling Focus",
        "focus mode_toggle",
        "Switch focus between tiling and floating windows.",
    ),
    _preset("Move Left", "move left", "Move the focused window to the left."),
    _preset("Move Right", "move right", "Move the focused window to the right."),
    _preset("Move Up", "move up", "Move the focused window up."),
    _preset("Move Down", "move down", "Move the focused window down."),
    _preset("Split Horizontal", "splith", "Split the focused container horizontally."),
    _preset("Split Vertical", "splitv", "Split the focused container vertically."),
    _preset("Layout Tabbed", "layout tabbed", "Show the focused container as tabs."),
    _preset("Layout Stacking", "layout stacking", "Show the focused container as a stack."),
    _preset("Layout Toggle Split", "layout toggle split", "Switch between split directions."),
    _preset("Workspace Next", "workspace next", "Switch to the next workspace."),
    _preset("Workspace Previous", "workspace prev", "Switch to the previous workspace."),
    _preset(
        "Workspace Back And Forth",
        "workspace back_and_forth",
        "Switch to the previously focused workspace.",
    ),
    _preset("Workspace 1", "workspace number", "Switch to workspace 1.", args="1"),
    _preset("Workspace 2", "workspace number", "Switch to workspace 2.", args="2"),
    _preset(
        "Move To Workspace 1",
        "move container to workspace number",
        "Move the focused window to workspace 1.",
        args="1",
    ),
    _preset(
        "Move To Workspace 2",
        "move container to workspace number",
        "Move the focused window to workspace 2.",
        args="2",
    ),
    _preset("Show Scratchpad", "scratchpad show", "Show or hide the scratchpad window."),
    _preset("Move To Scratchpad", "move scratchpad", "Move the focused window to the scratchpad."),
    CompositorActionPreset(
        label="Set Cursor",
        dispatcher="set_cursor_position",
        args="0 0",
        hint="Move the cursor to an absolute screen coordinate.",
        captures_position=True,
    ),
)

_SWAY_PRESET_LABELS = {
    (preset.dispatcher, preset.args): preset.label
    for preset in SWAY_DISPATCH_PRESETS
    if not preset.captures_position
}


def sway_action_label(action: MappingAction) -> str | None:
    dispatcher = " ".join(str(action.compositor_dispatcher or "").split())
    args = str(action.compositor_args or "").strip()
    if dispatcher == "set_cursor_position":
        return f"Set Cursor {args}".strip()
    return _SWAY_PRESET_LABELS.get((dispatcher, args))


SWAY_ACTION_DEFINITION = build_compositor_dispatch_definition(
    page_id="sway",
    compositor_id="sway",
    title="Sway",
    subtitle=("Send a Sway command through the active Sway listener using swaymsg syntax."),
    dispatcher_placeholder="e.g. workspace number",
    args_placeholder="e.g. 2",
    presets=SWAY_DISPATCH_PRESETS,
    allow_custom=True,
    action_label=sway_action_label,
)
