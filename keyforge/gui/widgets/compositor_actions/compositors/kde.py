from __future__ import annotations

from keyforge.common.models import ActionType, MappingAction
from keyforge.gui.widgets.compositor_actions.core import (
    CompositorActionDefinition,
    CompositorActionPreset,
)

KDE_DISPATCH_PRESETS = (
    CompositorActionPreset(
        label="Desktop Next",
        dispatcher="desktop_next",
        args="",
        hint="Switch to the next virtual desktop.",
    ),
    CompositorActionPreset(
        label="Desktop Previous",
        dispatcher="desktop_prev",
        args="",
        hint="Switch to the previous virtual desktop.",
    ),
    CompositorActionPreset(
        label="Close Window",
        dispatcher="window_close",
        args="",
        hint="Close the active window.",
    ),
    CompositorActionPreset(
        label="Fullscreen Toggle",
        dispatcher="fullscreen_toggle",
        args="",
        hint="Toggle fullscreen on the active window.",
    ),
    CompositorActionPreset(
        label="Focus Left",
        dispatcher="focus_left",
        args="",
        hint="Move focus to the window on the left.",
    ),
    CompositorActionPreset(
        label="Focus Right",
        dispatcher="focus_right",
        args="",
        hint="Move focus to the window on the right.",
    ),
    CompositorActionPreset(
        label="Focus Up",
        dispatcher="focus_up",
        args="",
        hint="Move focus to the window above.",
    ),
    CompositorActionPreset(
        label="Focus Down",
        dispatcher="focus_down",
        args="",
        hint="Move focus to the window below.",
    ),
    CompositorActionPreset(
        label="Move Left",
        dispatcher="move_left",
        args="",
        hint="Move the active window left.",
    ),
    CompositorActionPreset(
        label="Move Right",
        dispatcher="move_right",
        args="",
        hint="Move the active window right.",
    ),
    CompositorActionPreset(
        label="Move Up",
        dispatcher="move_up",
        args="",
        hint="Move the active window up.",
    ),
    CompositorActionPreset(
        label="Move Down",
        dispatcher="move_down",
        args="",
        hint="Move the active window down.",
    ),
    CompositorActionPreset(
        label="Tile Left",
        dispatcher="tile_left",
        args="",
        hint="Quick-tile the active window to the left side.",
    ),
    CompositorActionPreset(
        label="Tile Right",
        dispatcher="tile_right",
        args="",
        hint="Quick-tile the active window to the right side.",
    ),
    CompositorActionPreset(
        label="Tile Top",
        dispatcher="tile_top",
        args="",
        hint="Quick-tile the active window to the top half.",
    ),
    CompositorActionPreset(
        label="Tile Bottom",
        dispatcher="tile_bottom",
        args="",
        hint="Quick-tile the active window to the bottom half.",
    ),
    CompositorActionPreset(
        label="All Desktops Toggle",
        dispatcher="all_desktops_toggle",
        args="",
        hint="Toggle whether the active window appears on all desktops.",
    ),
    CompositorActionPreset(
        label="Show Desktop Toggle",
        dispatcher="show_desktop_toggle",
        args="",
        hint="Toggle Plasma's show-desktop mode.",
    ),
)

def _kde_available(current_action: MappingAction | None, status: dict[str, object]) -> bool:
    _ = current_action
    return bool(
        status.get("listener_name") == "kde"
        and status.get("compositor_dispatch_available") is True
    )


def _kde_fields(current_action: MappingAction | None) -> tuple[str, str]:
    if current_action is None or current_action.action_type != ActionType.COMPOSITOR_DISPATCH:
        return "", ""
    compositor_id = str(current_action.compositor_id or "").strip()
    if compositor_id and compositor_id != "kde":
        return "", ""
    return (
        str(current_action.compositor_dispatcher or ""),
        str(current_action.compositor_args or ""),
    )


def _build_kde_action(dispatcher: str, args: str) -> MappingAction:
    return MappingAction(
        action_type=ActionType.COMPOSITOR_DISPATCH,
        compositor_id="kde",
        compositor_dispatcher=dispatcher,
        compositor_args=args,
    )


def _describe_kde_action(action: MappingAction) -> str:
    return f"KDE Plasma → {action.compositor_dispatcher or '?'}"


KDE_ACTION_DEFINITION = CompositorActionDefinition(
    page_id="kde",
    compositor_id="kde",
    title="KDE Plasma",
    subtitle="Send a supported KWin action through the active KDE Plasma listener.",
    dispatcher_placeholder="e.g. tile_left",
    args_placeholder="No arguments supported",
    action_type=ActionType.COMPOSITOR_DISPATCH,
    presets=KDE_DISPATCH_PRESETS,
    allow_custom=False,
    is_available=_kde_available,
    extract_fields=_kde_fields,
    build_action=_build_kde_action,
    describe_action=_describe_kde_action,
)
