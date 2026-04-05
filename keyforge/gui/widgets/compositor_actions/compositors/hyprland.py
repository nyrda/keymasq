from __future__ import annotations

from keyforge.common.models import ActionType, MappingAction
from keyforge.gui.widgets.compositor_actions.core import (
    CompositorActionDefinition,
    CompositorActionPreset,
)

HYPRLAND_DISPATCH_PRESETS = (
    CompositorActionPreset(
        label="Toggle Floating",
        dispatcher="togglefloating",
        args="",
        hint="Toggle floating mode for the active window.",
    ),
    CompositorActionPreset(
        label="Fullscreen",
        dispatcher="fullscreen",
        args="0",
        hint="Toggle fullscreen for the active window.",
    ),
    CompositorActionPreset(
        label="Fake Fullscreen",
        dispatcher="fullscreen",
        args="1",
        hint="Toggle fake fullscreen for the active window.",
    ),
    CompositorActionPreset(
        label="Close Window",
        dispatcher="killactive",
        args="",
        hint="Close the active window.",
    ),
    CompositorActionPreset(
        label="Center Window",
        dispatcher="centerwindow",
        args="",
        hint="Center the active floating window.",
    ),
    CompositorActionPreset(
        label="Pin Window",
        dispatcher="pin",
        args="",
        hint="Pin the active window across workspaces.",
    ),
    CompositorActionPreset(
        label="Workspace Next",
        dispatcher="workspace",
        args="e+1",
        hint="Switch to the next workspace.",
    ),
    CompositorActionPreset(
        label="Workspace Previous",
        dispatcher="workspace",
        args="e-1",
        hint="Switch to the previous workspace.",
    ),
    CompositorActionPreset(
        label="Move To Special",
        dispatcher="movetoworkspace",
        args="special",
        hint="Move the active window to the special workspace.",
    ),
    CompositorActionPreset(
        label="Toggle Special",
        dispatcher="togglespecialworkspace",
        args="",
        hint="Show or hide the special workspace.",
    ),
    CompositorActionPreset(
        label="Focus Left",
        dispatcher="movefocus",
        args="l",
        hint="Focus the window on the left.",
    ),
    CompositorActionPreset(
        label="Focus Right",
        dispatcher="movefocus",
        args="r",
        hint="Focus the window on the right.",
    ),
    CompositorActionPreset(
        label="Focus Up",
        dispatcher="movefocus",
        args="u",
        hint="Focus the window above.",
    ),
    CompositorActionPreset(
        label="Focus Down",
        dispatcher="movefocus",
        args="d",
        hint="Focus the window below.",
    ),
    CompositorActionPreset(
        label="Move Window Left",
        dispatcher="movewindow",
        args="l",
        hint="Move the active window left.",
    ),
    CompositorActionPreset(
        label="Move Window Right",
        dispatcher="movewindow",
        args="r",
        hint="Move the active window right.",
    ),
    CompositorActionPreset(
        label="Move Window Up",
        dispatcher="movewindow",
        args="u",
        hint="Move the active window up.",
    ),
    CompositorActionPreset(
        label="Move Window Down",
        dispatcher="movewindow",
        args="d",
        hint="Move the active window down.",
    ),
)


def _hyprland_available(current_action: MappingAction | None, status: dict[str, object]) -> bool:
    _ = current_action
    return bool(
        status.get("listener_name") == "hyprland"
        and status.get("compositor_dispatch_available") is True
    )


def _hyprland_fields(current_action: MappingAction | None) -> tuple[str, str]:
    if current_action is None or current_action.action_type != ActionType.COMPOSITOR_DISPATCH:
        return "", ""
    compositor_id = str(current_action.compositor_id or "").strip()
    if compositor_id and compositor_id != "hyprland":
        return "", ""
    return (
        str(current_action.compositor_dispatcher or ""),
        str(current_action.compositor_args or ""),
    )


def _build_hyprland_action(dispatcher: str, args: str) -> MappingAction:
    return MappingAction(
        action_type=ActionType.COMPOSITOR_DISPATCH,
        compositor_id="hyprland",
        compositor_dispatcher=dispatcher,
        compositor_args=args,
    )


def _describe_hyprland_action(action: MappingAction) -> str:
    args = str(action.compositor_args or "").strip()
    suffix = f" {args}" if args else ""
    return f"Hyprland → {action.compositor_dispatcher or '?'}{suffix}"


HYPRLAND_ACTION_DEFINITION = CompositorActionDefinition(
    page_id="hyprland",
    compositor_id="hyprland",
    title="Hyprland",
    subtitle="Send a Hyprland dispatcher through the active Hyprland listener.",
    dispatcher_placeholder="e.g. togglefloating",
    args_placeholder="e.g. e+1, l, special",
    action_type=ActionType.COMPOSITOR_DISPATCH,
    presets=HYPRLAND_DISPATCH_PRESETS,
    allow_custom=True,
    is_available=_hyprland_available,
    extract_fields=_hyprland_fields,
    build_action=_build_hyprland_action,
    describe_action=_describe_hyprland_action,
)
