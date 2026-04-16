from __future__ import annotations

from keymasq.common.models import ActionType, MappingAction
from keymasq.gui.widgets.compositor_actions.core import (
    CompositorActionDefinition,
    CompositorActionPreset,
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
)


def _gnome_available(current_action: MappingAction | None, status: dict[str, object]) -> bool:
    _ = current_action
    return bool(
        status.get("listener_name") == "gnome"
        and status.get("compositor_dispatch_available") is True
    )


def _gnome_fields(current_action: MappingAction | None) -> tuple[str, str]:
    if current_action is None or current_action.action_type != ActionType.COMPOSITOR_DISPATCH:
        return "", ""
    compositor_id = str(current_action.compositor_id or "").strip()
    if compositor_id and compositor_id != "gnome":
        return "", ""
    return (
        str(current_action.compositor_dispatcher or ""),
        str(current_action.compositor_args or ""),
    )


def _build_gnome_action(dispatcher: str, args: str) -> MappingAction:
    return MappingAction(
        action_type=ActionType.COMPOSITOR_DISPATCH,
        compositor_id="gnome",
        compositor_dispatcher=dispatcher,
        compositor_args=args,
    )


def _describe_gnome_action(action: MappingAction) -> str:
    args = str(action.compositor_args or "").strip()
    suffix = f" {args}" if args else ""
    return f"GNOME → {action.compositor_dispatcher or '?'}{suffix}"


GNOME_ACTION_DEFINITION = CompositorActionDefinition(
    page_id="gnome",
    compositor_id="gnome",
    title="GNOME",
    subtitle="Send an allowlisted GNOME action through the active GNOME Shell bridge.",
    dispatcher_placeholder="e.g. workspace",
    args_placeholder="e.g. next, prev, 2, toggle",
    action_type=ActionType.COMPOSITOR_DISPATCH,
    presets=GNOME_DISPATCH_PRESETS,
    allow_custom=False,
    is_available=_gnome_available,
    extract_fields=_gnome_fields,
    build_action=_build_gnome_action,
    describe_action=_describe_gnome_action,
)
