"""Concrete widget tree for editing one Super Key draft."""

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import SuperkeyMode
from keymasq.common.model.superkeys import SuperkeyAction
from keymasq.gui.widgets.action_sequence_labels import (
    describe_mapping_editor_action,
    describe_superkey_editor_action,
)
from keymasq.gui.widgets.managed_editor.shell import LabeledForm
from keymasq.gui.widgets.superkey_editor.action_slot import ActionSlot
from keymasq.gui.widgets.superkey_editor.draft import SuperkeyDraft


class SuperkeyEditorView(Gtk.Box):
    """Own the editor widgets and translate them to and from drafts."""

    def __init__(
        self,
        *,
        modified: Callable[[], None],
        edit_pattern_slot: Callable[[ActionSlot[SuperkeyAction]], None],
        edit_overload_slot: Callable[[ActionSlot[MappingAction]], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._modified = modified
        self._mode_items = (SuperkeyMode.PATTERN, SuperkeyMode.OVERLOAD)

        self.append(self._build_fields_grid())
        self.append(Gtk.Separator())

        self.tap_slot = self._pattern_slot("Tap", "tap", edit_pattern_slot)
        self.double_tap_slot = self._pattern_slot(
            "Double Tap",
            "double_tap",
            edit_pattern_slot,
        )
        self.hold_slot = self._pattern_slot("Hold", "hold", edit_pattern_slot)
        self.tap_hold_slot = self._pattern_slot(
            "Tap + Hold",
            "tap_hold",
            edit_pattern_slot,
        )
        self.overload_slot = self._overload_slot(
            "Main Actions",
            "overload",
            edit_overload_slot,
            static_description="Held while pressed, released when you let go",
            tooltip=(
                "Main Actions start before On Press and stay held until after On Release, "
                "so they can provide a held modifier or context for both press/release lists."
            ),
        )
        self.overload_down_slot = self._overload_slot(
            "On Press",
            "overload_down",
            edit_overload_slot,
        )
        self.overload_up_slot = self._overload_slot(
            "On Release",
            "overload_up",
            edit_overload_slot,
        )

        actions_group = Adw.PreferencesGroup()
        actions_group.set_title("Actions")
        for slot in (
            self.tap_slot,
            self.double_tap_slot,
            self.hold_slot,
            self.tap_hold_slot,
            self.overload_slot,
        ):
            actions_group.add(slot.row)
        self.append(actions_group)

        self.overload_pulse_group = Adw.PreferencesGroup()
        self.overload_pulse_group.set_title("On Press / Release")
        self.overload_pulse_group.add(self.overload_down_slot.row)
        self.overload_pulse_group.add(self.overload_up_slot.row)
        self.append(self.overload_pulse_group)
        self.append(Gtk.Separator())

        self.timing_group = self._build_timing_group()
        self.append(self.timing_group)

        self.update_mode_visibility()

    @property
    def mode(self) -> SuperkeyMode:
        return self._mode_items[self.mode_dropdown.get_selected()]

    def focus_name(self) -> None:
        self.name_entry.grab_focus()

    def populate(self, draft: SuperkeyDraft) -> None:
        self.name_entry.set_text(draft.name)
        self.description_entry.set_text(draft.description)
        self.mode_dropdown.set_selected(self._mode_items.index(draft.mode))
        self.tap_slot.set_actions(draft.tap_actions)
        self.double_tap_slot.set_actions(draft.double_tap_actions)
        self.hold_slot.set_actions(draft.hold_actions)
        self.tap_hold_slot.set_actions(draft.tap_hold_actions)
        self.overload_slot.set_actions(draft.overload_actions)
        self.overload_down_slot.set_actions(draft.overload_down_actions)
        self.overload_up_slot.set_actions(draft.overload_up_actions)
        self.tap_timeout_spin.set_value(draft.tap_timeout_ms)
        self.double_tap_window_spin.set_value(draft.double_tap_window_ms)
        self.hold_threshold_spin.set_value(draft.hold_threshold_ms)
        self.update_mode_visibility()

    def draft(self) -> SuperkeyDraft:
        return SuperkeyDraft(
            name=self.name_entry.get_text(),
            description=self.description_entry.get_text(),
            mode=self.mode,
            tap_actions=tuple(self.tap_slot.actions),
            double_tap_actions=tuple(self.double_tap_slot.actions),
            hold_actions=tuple(self.hold_slot.actions),
            tap_hold_actions=tuple(self.tap_hold_slot.actions),
            overload_actions=tuple(self.overload_slot.actions),
            overload_down_actions=tuple(self.overload_down_slot.actions),
            overload_up_actions=tuple(self.overload_up_slot.actions),
            tap_timeout_ms=self.tap_timeout_spin.get_value_as_int(),
            double_tap_window_ms=self.double_tap_window_spin.get_value_as_int(),
            hold_threshold_ms=self.hold_threshold_spin.get_value_as_int(),
        )

    def update_mode_visibility(self) -> None:
        pattern_visible = self.mode == SuperkeyMode.PATTERN
        for slot in (
            self.tap_slot,
            self.double_tap_slot,
            self.hold_slot,
            self.tap_hold_slot,
        ):
            slot.set_visible(pattern_visible)
        overload_visible = not pattern_visible
        self.overload_slot.set_visible(overload_visible)
        self.overload_down_slot.set_visible(overload_visible)
        self.overload_up_slot.set_visible(overload_visible)
        self.overload_pulse_group.set_visible(overload_visible)
        self.timing_group.set_visible(pattern_visible)

    def _pattern_slot(
        self,
        title: str,
        action_key: str,
        edit_requested: Callable[[ActionSlot[SuperkeyAction]], None],
    ) -> ActionSlot[SuperkeyAction]:
        return ActionSlot(
            title=title,
            action_key=action_key,
            describe_action=describe_superkey_editor_action,
            edit_requested=edit_requested,
            modified=self._modified,
        )

    def _overload_slot(
        self,
        title: str,
        action_key: str,
        edit_requested: Callable[[ActionSlot[MappingAction]], None],
        *,
        static_description: str | None = None,
        tooltip: str | None = None,
    ) -> ActionSlot[MappingAction]:
        return ActionSlot(
            title=title,
            action_key=action_key,
            describe_action=describe_mapping_editor_action,
            edit_requested=edit_requested,
            modified=self._modified,
            static_description=static_description,
            tooltip=tooltip,
        )

    def _build_fields_grid(self) -> Gtk.Grid:
        form = LabeledForm()
        self.name_entry = Gtk.Entry()
        self.name_entry.set_hexpand(True)
        self.name_entry.connect("changed", self._on_modified)
        form.append("Name:", self.name_entry)

        self.description_entry = Gtk.Entry()
        self.description_entry.set_hexpand(True)
        self.description_entry.connect("changed", self._on_modified)
        form.append("Description:", self.description_entry)

        self.mode_dropdown = Gtk.DropDown.new_from_strings(["Pattern", "Overload"])
        self.mode_dropdown.connect("notify::selected", self._on_mode_changed)
        form.append("Mode:", self.mode_dropdown)
        return form.grid

    def _build_timing_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Timing")
        tap_timeout_row, self.tap_timeout_spin = self._build_timing_row(
            "Tap Timeout",
            "Maximum time for a tap (ms)",
            200,
            50,
            1000,
        )
        group.add(tap_timeout_row)
        double_tap_window_row, self.double_tap_window_spin = self._build_timing_row(
            "Double Tap Window",
            "Maximum time between taps (ms)",
            300,
            50,
            1000,
        )
        group.add(double_tap_window_row)
        hold_threshold_row, self.hold_threshold_spin = self._build_timing_row(
            "Hold Threshold",
            "Time to consider a hold (ms)",
            300,
            50,
            2000,
        )
        group.add(hold_threshold_row)
        return group

    def _build_timing_row(
        self,
        title: str,
        subtitle: str,
        default: int,
        lower: int,
        upper: int,
    ) -> tuple[Adw.ActionRow, Gtk.SpinButton]:
        row = Adw.ActionRow()
        row.set_title(title)
        row.set_subtitle(subtitle)
        adjustment = Gtk.Adjustment(
            value=default,
            lower=lower,
            upper=upper,
            step_increment=10,
        )
        spin = Gtk.SpinButton(adjustment=adjustment, climb_rate=0, digits=0)
        spin.set_numeric(True)
        spin.set_width_chars(5)
        spin.set_max_width_chars(5)
        spin.set_alignment(1.0)
        spin.connect("value-changed", self._on_modified)
        row.add_suffix(spin)
        return row, spin

    def _on_modified(self, *_args: object) -> None:
        self._modified()

    def _on_mode_changed(self, _dropdown: Gtk.DropDown, _param: object) -> None:
        self.update_mode_visibility()
        self._modified()
