from __future__ import annotations

from keymasq.gui.widgets.compositor_actions.core import (
    CompositorActionPreset,
    build_compositor_dispatch_definition,
)

NIRI_DISPATCH_PRESETS = (
    CompositorActionPreset(
        label="Close Window",
        dispatcher="close-window",
        args="",
        hint="Close the focused window.",
    ),
    CompositorActionPreset(
        label="Fullscreen",
        dispatcher="fullscreen-window",
        args="",
        hint="Toggle fullscreen on the focused window.",
    ),
    CompositorActionPreset(
        label="Fake Fullscreen",
        dispatcher="toggle-windowed-fullscreen",
        args="",
        hint="Toggle windowed fullscreen on the focused window.",
    ),
    CompositorActionPreset(
        label="Toggle Floating",
        dispatcher="toggle-window-floating",
        args="",
        hint="Toggle floating mode for the focused window.",
    ),
    CompositorActionPreset(
        label="Center Window",
        dispatcher="center-window",
        args="",
        hint="Center the focused floating window.",
    ),
    CompositorActionPreset(
        label="Previous Window",
        dispatcher="focus-column-left-or-last",
        args="",
        hint="Focus the previous window in Niri's scrolling order, looping to the end.",
    ),
    CompositorActionPreset(
        label="Next Window",
        dispatcher="focus-column-right-or-first",
        args="",
        hint="Focus the next window in Niri's scrolling order, looping to the start.",
    ),
    CompositorActionPreset(
        label="Focus Left",
        dispatcher="focus-column-left",
        args="",
        hint="Focus the column to the left.",
    ),
    CompositorActionPreset(
        label="Focus Right",
        dispatcher="focus-column-right",
        args="",
        hint="Focus the column to the right.",
    ),
    CompositorActionPreset(
        label="Focus Up",
        dispatcher="focus-window-up",
        args="",
        hint="Focus the window above.",
    ),
    CompositorActionPreset(
        label="Focus Down",
        dispatcher="focus-window-down",
        args="",
        hint="Focus the window below.",
    ),
    CompositorActionPreset(
        label="Move Column Left",
        dispatcher="move-column-left",
        args="",
        hint="Move the focused column to the left.",
    ),
    CompositorActionPreset(
        label="Move Column Right",
        dispatcher="move-column-right",
        args="",
        hint="Move the focused column to the right.",
    ),
    CompositorActionPreset(
        label="Move Window Up",
        dispatcher="move-window-up",
        args="",
        hint="Move the focused window up in its column.",
    ),
    CompositorActionPreset(
        label="Move Window Down",
        dispatcher="move-window-down",
        args="",
        hint="Move the focused window down in its column.",
    ),
    CompositorActionPreset(
        label="Workspace Up",
        dispatcher="focus-workspace-up",
        args="",
        hint="Focus the workspace above.",
    ),
    CompositorActionPreset(
        label="Workspace Down",
        dispatcher="focus-workspace-down",
        args="",
        hint="Focus the workspace below.",
    ),
    CompositorActionPreset(
        label="Workspace Previous",
        dispatcher="focus-workspace-previous",
        args="",
        hint="Focus the previously focused workspace.",
    ),
    CompositorActionPreset(
        label="Workspace 1",
        dispatcher="focus-workspace",
        args="1",
        hint="Focus workspace 1.",
    ),
    CompositorActionPreset(
        label="Workspace 2",
        dispatcher="focus-workspace",
        args="2",
        hint="Focus workspace 2.",
    ),
    CompositorActionPreset(
        label="Move To Workspace 1",
        dispatcher="move-window-to-workspace",
        args="1",
        hint="Move the focused window to workspace 1 and follow it.",
    ),
    CompositorActionPreset(
        label="Move To Workspace 2",
        dispatcher="move-window-to-workspace",
        args="2",
        hint="Move the focused window to workspace 2 and follow it.",
    ),
    CompositorActionPreset(
        label="Send To Workspace 1",
        dispatcher="send-window-to-workspace",
        args="1",
        hint="Send the focused window to workspace 1 without following it.",
    ),
    CompositorActionPreset(
        label="Send To Workspace 2",
        dispatcher="send-window-to-workspace",
        args="2",
        hint="Send the focused window to workspace 2 without following it.",
    ),
)


NIRI_ACTION_DEFINITION = build_compositor_dispatch_definition(
    page_id="niri",
    compositor_id="niri",
    title="Niri",
    subtitle="Send a Niri action through the active Niri listener using niri msg action syntax.",
    dispatcher_placeholder="e.g. focus-workspace",
    args_placeholder="e.g. 2, --id 17, --focus",
    presets=NIRI_DISPATCH_PRESETS,
    allow_custom=True,
)
