from __future__ import annotations

from keymasq.gui.widgets.compositor_actions.core import (
    CompositorActionPreset,
    build_compositor_dispatch_definition,
)

GNOME_DISPATCH_PRESETS = (
    CompositorActionPreset(
        label="Workspace Next",
        dispatcher="workspace",
        args="next",
        hint="Switch to the next GNOME workspace.",
    ),
    CompositorActionPreset(
        label="Workspace Previous",
        dispatcher="workspace",
        args="prev",
        hint="Switch to the previous GNOME workspace.",
    ),
    CompositorActionPreset(
        label="Workspace 1",
        dispatcher="workspace",
        args="1",
        hint="Switch to GNOME workspace 1.",
    ),
    CompositorActionPreset(
        label="Workspace 2",
        dispatcher="workspace",
        args="2",
        hint="Switch to GNOME workspace 2.",
    ),
    CompositorActionPreset(
        label="Move To Workspace 1",
        dispatcher="move_to_workspace",
        args="1",
        hint="Move the focused window to GNOME workspace 1 and switch there.",
    ),
    CompositorActionPreset(
        label="Move To Workspace 2",
        dispatcher="move_to_workspace",
        args="2",
        hint="Move the focused window to GNOME workspace 2 and switch there.",
    ),
    CompositorActionPreset(
        label="Close Window",
        dispatcher="close_active",
        args="",
        hint="Close the focused GNOME window.",
    ),
    CompositorActionPreset(
        label="Toggle Fullscreen",
        dispatcher="fullscreen",
        args="toggle",
        hint="Toggle fullscreen on the focused GNOME window.",
    ),
    CompositorActionPreset(
        label="Toggle Maximize",
        dispatcher="maximize",
        args="toggle",
        hint="Toggle maximize on the focused GNOME window.",
    ),
    CompositorActionPreset(
        label="Set Cursor",
        dispatcher="set_cursor_position",
        args="0 0",
        hint="Move the GNOME pointer to an absolute screen coordinate.",
        captures_position=True,
    ),
)


GNOME_ACTION_DEFINITION = build_compositor_dispatch_definition(
    page_id="gnome",
    compositor_id="gnome",
    title="GNOME",
    subtitle="Send an allowlisted GNOME action through the active GNOME Shell bridge.",
    dispatcher_placeholder="e.g. workspace",
    args_placeholder="e.g. next, prev, 2, toggle",
    presets=GNOME_DISPATCH_PRESETS,
    allow_custom=False,
)
