"""Shared GTK builders used by the analog-control editor view."""

from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw  # pyright: ignore[reportAttributeAccessIssue]


@dataclass(frozen=True, slots=True)
class DigitalPanelCallbacks:
    add_range: Callable[[], None]


@dataclass(frozen=True, slots=True)
class TemplatePanelCallbacks:
    apply_wasd: Callable[[], None]
    apply_arrows: Callable[[], None]
    apply_mouse_wheel: Callable[[], None]


@dataclass(slots=True)
class DigitalGroupHandle:
    group: Adw.PreferencesGroup
    add_range_row: Adw.ActionRow


@dataclass(slots=True)
class TemplateGroupHandle:
    group: Adw.PreferencesGroup
    action_rows: tuple[Adw.ActionRow, ...]


def _run_action(_row: Adw.ActionRow, action: Callable[[], None]) -> None:
    action()


def build_digital_group(callbacks: DigitalPanelCallbacks) -> DigitalGroupHandle:
    group = Adw.PreferencesGroup(title="Digital Action Ranges")
    add_range_row = Adw.ActionRow(
        title="+ Add Range",
        subtitle="Create a new editable activation and release range",
    )
    add_range_row.set_activatable(True)
    add_range_row.connect("activated", _run_action, callbacks.add_range)
    group.add(add_range_row)
    return DigitalGroupHandle(group=group, add_range_row=add_range_row)


def build_template_group(callbacks: TemplatePanelCallbacks) -> TemplateGroupHandle:
    group = Adw.PreferencesGroup(
        title="Range Templates",
        description=(
            "Templates append editable digital ranges. They do not create special runtime modes."
        ),
    )
    template_rows: tuple[tuple[str, str, Callable[[], None]], ...] = (
        (
            "Apply WASD Template",
            "Adds four keyboard ranges for left-stick movement",
            callbacks.apply_wasd,
        ),
        ("Apply Arrow Keys Template", "Adds four arrow-key ranges", callbacks.apply_arrows),
        (
            "Apply Mouse Wheel Template",
            "Adds two rapidfire wheel ranges",
            callbacks.apply_mouse_wheel,
        ),
    )
    action_rows: list[Adw.ActionRow] = []
    for title, subtitle, callback in template_rows:
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        row.set_activatable(True)
        row.connect("activated", _run_action, callback)
        group.add(row)
        action_rows.append(row)
    return TemplateGroupHandle(group=group, action_rows=tuple(action_rows))
