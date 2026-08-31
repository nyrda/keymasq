import shlex
from collections.abc import Callable, Sequence
from typing import Any, cast

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.hardware import AnalogInputDefinition, ButtonDefinition, HardwareConfig
from keymasq.gui.widgets.action_labels import describe_mapping_action_compact
from keymasq.session.profile.manager import ProfileManager
from keymasq.session.profile.types import ProfileInfo

_ACTION_SUMMARY_MARKER = "..."


def _char_middle_shorten_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= len(_ACTION_SUMMARY_MARKER):
        return text[:max_chars]

    budget = max_chars - len(_ACTION_SUMMARY_MARKER)
    head_len = max(1, (budget + 1) // 2)
    tail_len = max(1, budget - head_len)
    return f"{text[:head_len]}{_ACTION_SUMMARY_MARKER}{text[-tail_len:]}"


def _compact_exec_summary(text: str, max_chars: int) -> str | None:
    prefix = "▶ "
    if not text.startswith(prefix):
        return None

    command = text[len(prefix) :].strip()
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if len(parts) < 3:
        return None

    positional = [part for part in parts[1:] if not part.startswith("-")]
    if not positional:
        return None

    compact = f"{prefix}{parts[0]} {' '.join(positional)}"
    if len(compact) <= max_chars:
        return compact
    return None


def _display_action_summary(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text

    compact_exec = _compact_exec_summary(text, max_chars)
    if compact_exec is not None:
        return compact_exec

    return _char_middle_shorten_text(text, max_chars)


def profile_info_by_name(
    profile_manager: ProfileManager | None,
    profiles: Sequence[ProfileInfo | None],
    profile_name: str,
) -> ProfileInfo | None:
    if profile_manager:
        return profile_manager.get_profile(profile_name)
    for profile in profiles:
        if profile and profile.config.name == profile_name:
            return profile
    return None


def get_effective_mapping_for_button(
    *,
    active_profile_names: Sequence[str],
    profile_lookup: Callable[[str], ProfileInfo | None],
    hardware_id: str,
    button_id: str,
) -> tuple[str | None, MappingAction | None]:
    winner_profile_name: str | None = None
    winner_mapping: MappingAction | None = None

    for profile_name in active_profile_names:
        profile = profile_lookup(profile_name)
        if profile is None:
            continue
        layer = profile.config.get_layer(hardware_id)
        if layer is None:
            continue
        mapping = layer.mappings.get(button_id)
        if mapping is None:
            continue
        winner_profile_name = profile_name
        winner_mapping = mapping

    return winner_profile_name, winner_mapping


def describe_mapping(
    mapping: MappingAction,
    *,
    describe_passthrough: Callable[[ButtonDefinition], str],
    button: ButtonDefinition | None = None,
) -> str:
    if mapping.action_type == ActionType.PASSTHROUGH and button is not None:
        return describe_passthrough(button)
    return describe_mapping_action_compact(mapping, include_state=True)


def describe_passthrough_output(
    button: ButtonDefinition,
    *,
    label_from_evdev: Callable[[str], str],
) -> str:
    if button.evdev in {"rel_wheel", "rel_hwheel"} and button.evdev_value is not None:
        if button.evdev == "rel_wheel":
            return "↑ Scroll Up" if button.evdev_value > 0 else "↓ Scroll Down"
        return "→ Scroll Right" if button.evdev_value > 0 else "← Scroll Left"
    return f"→ {label_from_evdev(button.evdev)}"


def set_action_label_text(
    label: Gtk.Label,
    text: str,
    *,
    max_chars: int,
    pre_shorten: bool = True,
) -> None:
    display_text = _display_action_summary(text, max_chars) if pre_shorten else text
    label.set_text(display_text)
    needs_tooltip = display_text != text or (not pre_shorten and len(text) > max_chars)
    label.set_tooltip_text(text if needs_tooltip else None)


def update_button_display(
    *,
    button_widgets: dict[str, Gtk.Button],
    button_id: str,
    device: HardwareConfig,
    selected_layer: Any,
    selected_profile: ProfileInfo | None,
    effective_mapping: tuple[str | None, MappingAction | None],
    describe_mapping_for_button: Callable[[MappingAction, ButtonDefinition | None], str],
    describe_passthrough: Callable[[ButtonDefinition], str],
    action_summary_chars: int,
) -> None:
    widget = button_widgets.get(button_id)
    if not widget:
        return

    action_label = cast(Gtk.Label, widget._action_label)
    name_label = cast(Gtk.Label, widget._name_label)
    mapping = None

    if selected_layer:
        mapping = selected_layer.mappings.get(button_id)

    button = next(
        (candidate for candidate in device.buttons if candidate.id == button_id),
        None,
    )
    analog = next(
        (candidate for candidate in device.analog_inputs if candidate.id == button_id),
        None,
    )
    motion = next(
        (candidate for candidate in device.motion_sensors if candidate.id == button_id),
        None,
    )
    if button is None and analog is None and motion is None:
        return

    winner_profile_name, winner_mapping = effective_mapping

    for cls in (
        "button-card-mapped",
        "button-card-mapped-active",
        "button-card-mapped-inactive",
        "button-card-passthrough",
    ):
        widget.remove_css_class(cls)
    for cls in ("success", "dim-label"):
        action_label.remove_css_class(cls)
        name_label.remove_css_class(cls)

    if mapping:
        description = describe_mapping_for_button(mapping, button)
        set_action_label_text(
            action_label,
            description,
            max_chars=action_summary_chars,
            pre_shorten=motion is None,
        )
        if selected_profile and winner_profile_name == selected_profile.config.name:
            action_label.add_css_class("success")
            widget.add_css_class("button-card-mapped-active")
            if winner_mapping is not None:
                widget.set_tooltip_text("Currently active binding")
            else:
                widget.set_tooltip_text(None)
        else:
            action_label.add_css_class("dim-label")
            name_label.add_css_class("dim-label")
            widget.add_css_class("button-card-mapped-inactive")
            if winner_profile_name and winner_mapping is not None:
                widget.set_tooltip_text(
                    f"Active binding: {describe_mapping_for_button(winner_mapping, button)} "
                    f"from {winner_profile_name}"
                )
            else:
                widget.set_tooltip_text("This binding is not currently active")
    else:
        passthrough_label = _passthrough_label(
            button,
            analog,
            describe_passthrough,
            motion=motion is not None,
        )
        set_action_label_text(
            action_label,
            passthrough_label,
            max_chars=action_summary_chars,
            pre_shorten=motion is None,
        )
        action_label.add_css_class("dim-label")
        widget.add_css_class("button-card-passthrough")
        if winner_profile_name and winner_mapping is not None:
            widget.set_tooltip_text(
                f"Active binding: {describe_mapping_for_button(winner_mapping, button)} "
                f"from {winner_profile_name}"
            )
        else:
            widget.set_tooltip_text(None)


def _passthrough_label(
    button: ButtonDefinition | None,
    analog: AnalogInputDefinition | None,
    describe_passthrough: Callable[[ButtonDefinition], str],
    *,
    motion: bool = False,
) -> str:
    if button is not None:
        return describe_passthrough(button)
    if analog is not None and analog.type == "axis":
        return "Axis passthrough"
    if motion:
        return "Motion passthrough"
    return "Analog passthrough"
