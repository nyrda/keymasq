from __future__ import annotations

import re

from keymasq.common.model.actions import MappingAction
from keymasq.gui.widgets.compositor_actions.core import (
    CompositorActionPreset,
    build_compositor_dispatch_definition,
)


def _workspace_preset(index: int) -> CompositorActionPreset:
    return CompositorActionPreset(
        label=f"Workspace {index}",
        dispatcher=f'hl.dsp.focus({{ workspace = "{index}" }})',
        args="",
        hint=f"Switch to workspace {index}.",
    )


HYPRLAND_DISPATCH_PRESETS = (
    CompositorActionPreset(
        label="Toggle Floating",
        dispatcher='hl.dsp.window.float({ action = "toggle" })',
        args="",
        hint="Toggle floating mode for the active window.",
    ),
    CompositorActionPreset(
        label="Fullscreen",
        dispatcher='hl.dsp.window.fullscreen({ mode = "fullscreen", action = "toggle" })',
        args="",
        hint="Toggle fullscreen for the active window.",
    ),
    CompositorActionPreset(
        label="Fake Fullscreen",
        dispatcher='hl.dsp.window.fullscreen({ mode = "maximized", action = "toggle" })',
        args="",
        hint="Toggle fake fullscreen for the active window.",
    ),
    CompositorActionPreset(
        label="Close Window",
        dispatcher="hl.dsp.window.close()",
        args="",
        hint="Close the active window.",
    ),
    CompositorActionPreset(
        label="Center Window",
        dispatcher="hl.dsp.window.center()",
        args="",
        hint="Center the active floating window.",
    ),
    CompositorActionPreset(
        label="Pin Window",
        dispatcher='hl.dsp.window.pin({ action = "toggle" })',
        args="",
        hint="Pin the active window across workspaces.",
    ),
    CompositorActionPreset(
        label="Workspace Next",
        dispatcher='hl.dsp.focus({ workspace = "e+1" })',
        args="",
        hint="Switch to the next workspace.",
    ),
    CompositorActionPreset(
        label="Workspace Previous",
        dispatcher='hl.dsp.focus({ workspace = "e-1" })',
        args="",
        hint="Switch to the previous workspace.",
    ),
    *(_workspace_preset(index) for index in range(1, 7)),
    CompositorActionPreset(
        label="Move To Special",
        dispatcher='hl.dsp.window.move({ workspace = "special", follow = true })',
        args="",
        hint="Move the active window to the special workspace.",
    ),
    CompositorActionPreset(
        label="Toggle Special",
        dispatcher='hl.dsp.workspace.toggle_special("")',
        args="",
        hint="Show or hide the special workspace.",
    ),
    CompositorActionPreset(
        label="Focus Left",
        dispatcher='hl.dsp.focus({ direction = "l" })',
        args="",
        hint="Focus the window on the left.",
    ),
    CompositorActionPreset(
        label="Focus Right",
        dispatcher='hl.dsp.focus({ direction = "r" })',
        args="",
        hint="Focus the window on the right.",
    ),
    CompositorActionPreset(
        label="Focus Up",
        dispatcher='hl.dsp.focus({ direction = "u" })',
        args="",
        hint="Focus the window above.",
    ),
    CompositorActionPreset(
        label="Focus Down",
        dispatcher='hl.dsp.focus({ direction = "d" })',
        args="",
        hint="Focus the window below.",
    ),
    CompositorActionPreset(
        label="Move Window Left",
        dispatcher='hl.dsp.window.move({ direction = "l" })',
        args="",
        hint="Move the active window left.",
    ),
    CompositorActionPreset(
        label="Move Window Right",
        dispatcher='hl.dsp.window.move({ direction = "r" })',
        args="",
        hint="Move the active window right.",
    ),
    CompositorActionPreset(
        label="Move Window Up",
        dispatcher='hl.dsp.window.move({ direction = "u" })',
        args="",
        hint="Move the active window up.",
    ),
    CompositorActionPreset(
        label="Move Window Down",
        dispatcher='hl.dsp.window.move({ direction = "d" })',
        args="",
        hint="Move the active window down.",
    ),
    CompositorActionPreset(
        label="Set Cursor",
        dispatcher="set_cursor_position",
        args="0 0",
        hint="Move the compositor cursor to an absolute screen coordinate.",
        captures_position=True,
    ),
)

_WORKSPACE_DISPATCH_RE = re.compile(
    r"^hl\.dsp\.focus\(\{\s*workspace\s*=\s*(?:\"([^\"]+)\"|([^,\s}]+))\s*\}\)$"
)
_MOVE_WORKSPACE_DISPATCH_RE = re.compile(
    r"^hl\.dsp\.window\.move\(\{\s*"
    r"workspace\s*=\s*(?:\"([^\"]+)\"|([^,\s}]+))\s*,\s*"
    r"follow\s*=\s*(true|false)\s*"
    r"\}\)$"
)


def _normalized_dispatcher(value: str) -> str:
    return " ".join(value.strip().split())


_HYPRLAND_PRESET_LABELS = {
    _normalized_dispatcher(preset.dispatcher): preset.label
    for preset in HYPRLAND_DISPATCH_PRESETS
    if not preset.captures_position
}


def hyprland_action_label(action: MappingAction) -> str | None:
    dispatcher = _normalized_dispatcher(str(action.compositor_dispatcher or ""))
    args = str(action.compositor_args or "").strip()
    if dispatcher == "set_cursor_position":
        return f"Set Cursor {args}".strip()
    if args:
        return None
    preset_label = _HYPRLAND_PRESET_LABELS.get(dispatcher)
    if preset_label is not None:
        return preset_label
    workspace_label: str | None = None
    workspace_match = _WORKSPACE_DISPATCH_RE.fullmatch(dispatcher)
    if workspace_match is not None:
        workspace = workspace_match.group(1) or workspace_match.group(2) or "?"
        workspace_label = f"workspace {workspace}"
    move_workspace_match = _MOVE_WORKSPACE_DISPATCH_RE.fullmatch(dispatcher)
    if move_workspace_match is not None:
        workspace = move_workspace_match.group(1) or move_workspace_match.group(2) or "?"
        verb = "Move To" if move_workspace_match.group(3) == "true" else "Send To"
        if workspace.casefold() == "special":
            return f"{verb} Special"
        return f"{verb} Workspace {workspace}"
    if workspace_label is not None:
        return workspace_label
    return None


HYPRLAND_ACTION_DEFINITION = build_compositor_dispatch_definition(
    page_id="hyprland",
    compositor_id="hyprland",
    title="Hyprland",
    subtitle="Send a Hyprland dispatcher through the active Hyprland listener.",
    dispatcher_placeholder="e.g. hl.dsp.window.float()",
    args_placeholder="leave empty for Lua dispatch",
    presets=HYPRLAND_DISPATCH_PRESETS,
    allow_custom=True,
    args_visible=False,
    show_fields_for_presets=False,
    action_label=hyprland_action_label,
)
