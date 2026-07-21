from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.combos import normalize_combo_evdev
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.profiles import ComboConfig, ComboEvent, ComboStep
from keymasq.gui.widgets.action_labels import describe_mapping_action_compact
from keymasq.gui.widgets.fuzzy_search import fuzzy_query_matches
from keymasq.gui.widgets.key_selector.targets import EVDEV_TO_GAMEPAD, EVDEV_TO_KEY


@dataclass(frozen=True)
class ComboSearchDocument:
    visible_fields: tuple[str, ...] = ()
    supplemental_fields: tuple[str, ...] = ()


def sort_combo_keys(keys: list[str]) -> list[str]:
    modifier_order = {
        "ctrl": 0,
        "shift": 1,
        "alt": 2,
        "meta": 3,
        "key_menu": 4,
    }
    return sorted(
        [normalize_combo_evdev(key) for key in keys],
        key=lambda key: (modifier_order.get(key, 100), combo_key_label(key).casefold()),
    )


def combo_key_label(key: str) -> str:
    key = normalize_combo_evdev(key)
    if key in EVDEV_TO_KEY:
        return EVDEV_TO_KEY[key]
    if key in EVDEV_TO_GAMEPAD:
        return EVDEV_TO_GAMEPAD[key]
    if key == "ctrl":
        return "Ctrl"
    if key == "shift":
        return "Shift"
    if key == "alt":
        return "Alt"
    if key == "meta":
        return "Meta"
    if key == "wheel_up":
        return "Scroll Up"
    if key == "wheel_down":
        return "Scroll Down"
    if key == "wheel_left":
        return "Scroll Left"
    if key == "wheel_right":
        return "Scroll Right"

    token = key.upper()
    if token.startswith("KEY_"):
        token = token[4:]
    if token.startswith("BTN_"):
        token = token[4:]
    token = token.replace("LEFTCTRL", "LCtrl").replace("RIGHTCTRL", "RCtrl")
    token = token.replace("LEFTSHIFT", "LShift").replace("RIGHTSHIFT", "RShift")
    token = token.replace("LEFTALT", "LAlt").replace("RIGHTALT", "RAlt")
    token = token.replace("LEFTMETA", "LMeta").replace("RIGHTMETA", "RMeta")
    token = token.replace("PAGEUP", "PgUp").replace("PAGEDOWN", "PgDn")
    return token.replace("_", " ").title()


def combo_step_event_key(event: ComboEvent) -> str:
    return normalize_combo_evdev(event.evdev)


def combo_event_sort_key(event: ComboEvent) -> str:
    return sort_combo_keys([event.evdev])[0]


def combo_step_label(step: ComboStep) -> str:
    return "+".join(
        combo_key_label(key)
        for key in sort_combo_keys([combo_step_event_key(event) for event in step.events])
    )


def combo_trigger_label(steps: list[ComboStep]) -> str:
    return " -> ".join(combo_step_label(step) for step in steps if step.events)


def combo_restore_key_options(steps: list[ComboStep]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for step in steps:
        for key in sort_combo_keys([combo_step_event_key(event) for event in step.events]):
            if key in seen:
                continue
            seen.add(key)
            ordered.append(key)
    return ordered


def combo_action_label(action: MappingAction | None) -> str:
    if action is None:
        return ""
    if action.action_type == ActionType.KEYBOARD:
        return combo_key_label(action.target or "?")
    if action.action_type == ActionType.MOUSE:
        return combo_key_label(action.target or "?")
    if action.action_type == ActionType.GAMEPAD:
        return combo_key_label(action.target or "?")
    if action.action_type == ActionType.GAMEPAD_AXIS:
        return f"{action.target or '?'}={int(action.axis_value)}"
    if action.action_type == ActionType.EXEC:
        return action.cmd or "Exec"
    if action.action_type == ActionType.COMPOSITOR_DISPATCH:
        label = describe_mapping_action_compact(action)
        return label.removeprefix("🪟 ").strip()
    if action.action_type == ActionType.SUPPRESS:
        return "Suppress"
    if action.action_type == ActionType.MACRO:
        return action.macro_name or "Macro"
    if action.action_type == ActionType.REPEAT:
        return "Repeat Last"
    if action.action_type == ActionType.PROFILE_ENABLE:
        return f"Enable {action.profile_name or '?'}{_profile_lifetime_suffix(action)}"
    if action.action_type == ActionType.PROFILE_DISABLE:
        return f"Disable {action.profile_name or '?'}"
    if action.action_type == ActionType.PROFILE_TOGGLE:
        return f"Toggle {action.profile_name or '?'}{_profile_lifetime_suffix(action)}"
    if action.action_type == ActionType.START_MACRO_RECORDING:
        return "Start Recording"
    if action.action_type == ActionType.STOP_MACRO_RECORDING:
        return "Stop Recording"
    if action.action_type == ActionType.PLAY_MACRO_SLOT:
        slot = f" {action.macro_recording_slot}" if action.macro_recording_slot else ""
        return f"Play Slot{slot}"
    if action.action_type == ActionType.CANCEL_MACRO_PLAYBACK:
        return "Cancel Playback"
    if action.action_type == ActionType.EMERGENCY_RESET:
        return "Emergency Reset"
    if action.action_type == ActionType.MOUSE_MOVE_REL:
        return f"Move {action.move_x}, {action.move_y}"
    if action.action_type == ActionType.MOUSE_MOVE_ABS:
        return f"Move Abs {action.move_x}, {action.move_y}"
    if action.action_type == ActionType.MOUSE_MOVE_NATURAL_ABS:
        return f"Move Natural {action.move_x}, {action.move_y}"
    if action.action_type == ActionType.SUPERKEY:
        return action.superkey_name or "Super Key"
    return action.action_type.value.replace("_", " ").title()


def _profile_lifetime_suffix(action: MappingAction) -> str:
    policy = action.profile_deactivation
    if policy is None:
        return ""
    if policy.on_trigger_end and policy.after_actions is None and policy.timeout_ms is None:
        return " (while held)"
    if not policy.on_trigger_end and policy.timeout_ms is None and policy.after_actions == 1:
        return " (one-shot)"
    if not policy.on_trigger_end and policy.timeout_ms is None and policy.after_actions:
        return f" ({int(policy.after_actions)} actions)"
    if not policy.on_trigger_end and policy.after_actions is None and policy.timeout_ms:
        return f" ({int(policy.timeout_ms)} ms)"
    return " (custom)"


def combo_default_name(combo: ComboConfig) -> str:
    trigger = combo_trigger_label(combo.steps)
    action = combo_action_label(combo.action)
    if trigger and action:
        return f"{trigger} -> {action}"
    if trigger:
        return trigger
    if action:
        return action
    return "Combo"


def combo_search_document(
    combo: ComboConfig,
    *,
    profile_name: str = "",
    additional_event_fields: Sequence[str] = (),
) -> ComboSearchDocument:
    visible_parts = [
        combo.name or combo_default_name(combo),
        combo_trigger_label(combo.steps),
        describe_mapping_action_compact(combo.action),
        profile_name,
    ]
    supplemental_fields: list[str] = []
    for step in combo.steps:
        if step.timeout_ms is not None:
            supplemental_fields.append(f"timeout {int(step.timeout_ms)}ms")
        for event in step.events:
            supplemental_fields.extend(
                field
                for field in (
                    f"{combo_key_label(event.evdev)} {event.evdev}",
                    event.hardware_id,
                    event.source or "",
                )
                if field.strip()
            )
    if combo.recall_trigger_keys:
        supplemental_fields.append("recall trigger keys")
    if combo.restore_trigger_keys:
        supplemental_fields.append(
            "restore trigger keys " + " ".join(combo.restore_trigger_keys)
        )
    if combo.match_across_devices:
        supplemental_fields.append("any device across devices")
    supplemental_fields.extend(
        str(field) for field in additional_event_fields if str(field or "").strip()
    )
    return ComboSearchDocument(
        visible_fields=tuple(
            str(part) for part in visible_parts if str(part or "").strip()
        ),
        supplemental_fields=tuple(supplemental_fields),
    )


def combo_search_matches(query: str, document: ComboSearchDocument) -> bool:
    if fuzzy_query_matches(query, ""):
        return True
    visible_text = " ".join(document.visible_fields)
    return fuzzy_query_matches(query, visible_text) or any(
        fuzzy_query_matches(query, field) for field in document.supplemental_fields
    )


def combo_row_search_matches(query: str, row: Gtk.ListBoxRow) -> bool:
    document = getattr(row, "_combo_search_document", None)
    if not isinstance(document, ComboSearchDocument):
        return fuzzy_query_matches(query, "")
    return combo_search_matches(query, document)


def create_combo_summary_row(
    *,
    name: str,
    steps: Sequence[ComboStep],
    action: MappingAction | None,
    subtitle: str = "",
    read_only: bool = False,
    tooltip: str = "",
    step_tooltips: Sequence[str] | None = None,
    trailing_widget: Gtk.Widget | None = None,
) -> Gtk.ListBoxRow:
    row = Gtk.ListBoxRow()
    if read_only:
        row.set_selectable(False)

    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    box.add_css_class("combo-row")
    if read_only:
        box.add_css_class("combo-row-readonly")

    if subtitle:
        name_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name_box.set_hexpand(True)
        name_box.set_halign(Gtk.Align.START)

        name_label = _combo_row_name_label(name)
        name_box.append(name_label)

        subtitle_label = Gtk.Label(label=subtitle)
        subtitle_label.set_halign(Gtk.Align.START)
        subtitle_label.set_xalign(0.0)
        subtitle_label.set_ellipsize(Pango.EllipsizeMode.END)
        subtitle_label.add_css_class("caption")
        subtitle_label.add_css_class("dim-label")
        name_box.append(subtitle_label)
        box.append(name_box)
    else:
        name_label = _combo_row_name_label(name)
        name_label.set_hexpand(True)
        box.append(name_label)

    trigger_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
    trigger_box.set_halign(Gtk.Align.START)
    for index, step in enumerate(steps):
        pill = Gtk.Label(label=combo_step_label(step))
        pill.add_css_class("combo-step-pill")
        if step_tooltips is not None and index < len(step_tooltips):
            pill.set_tooltip_text(step_tooltips[index])
        trigger_box.append(pill)
        if index < len(steps) - 1:
            arrow = Gtk.Label(label="\u2192")
            arrow.add_css_class("dim-label")
            trigger_box.append(arrow)
    box.append(trigger_box)

    action_label = Gtk.Label(label=describe_mapping_action_compact(action))
    action_label.set_width_chars(22)
    action_label.set_max_width_chars(22)
    action_label.set_ellipsize(Pango.EllipsizeMode.END)
    action_label.set_halign(Gtk.Align.END)
    action_label.set_xalign(1.0)
    action_label.add_css_class("dim-label")
    action_label.add_css_class("caption")
    box.append(action_label)

    if trailing_widget is not None:
        box.append(trailing_widget)

    row.set_child(box)
    if tooltip:
        row.set_tooltip_text(tooltip)
    return row


def _combo_row_name_label(name: str) -> Gtk.Label:
    name_label = Gtk.Label(label=name)
    name_label.set_halign(Gtk.Align.START)
    name_label.set_xalign(0.0)
    name_label.set_ellipsize(Pango.EllipsizeMode.END)
    name_label.add_css_class("heading")
    return name_label
