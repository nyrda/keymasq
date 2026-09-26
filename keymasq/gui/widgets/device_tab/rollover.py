"""Rollover group creation, selection mode, and editing in device tabs.

Groups belong to a profile and can span devices. Selection mode and the group
editor belong to the window, so the user can switch device tabs while picking
keys. Every device tab paints its own cells from that shared state.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.hardware import ButtonDefinition
from keymasq.common.model.profiles import RolloverGroup, RolloverMember
from keymasq.common.rollover import (
    default_rollover_group_name,
    rollover_group_for_member,
)
from keymasq.gui.widgets.device_tab.rollover_dialog import (
    RolloverDialogCallbacks,
    RolloverGroupDialog,
    RolloverMemberInfo,
)
from keymasq.gui.widgets.device_tab.rollover_state import (
    RolloverSelection,
    axis_display_name,
    selection_block_reason,
    selection_summary,
    suggest_axis_rollover_members,
)
from keymasq.session.profile.types import ProfileInfo

_SELECTED_CLASS = "rollover-selected"
_UNAVAILABLE_CLASS = "rollover-unavailable"


@dataclass
class RolloverWindowState:
    """Rollover UI state shared by every device tab of one window."""

    selection: RolloverSelection | None = None
    dialog: RolloverGroupDialog | None = None
    # A member of the group the open editor shows. Saving can change the
    # group's name and members, so the editor finds its group through this.
    dialog_anchor: RolloverMember | None = None


class RolloverMixin:
    """Device-tab controller for rollover groups in the selected profile."""

    def _setup_rollover_widgets(self: Any) -> None:
        if getattr(self, "_local_rollover_state", None) is None:
            self._local_rollover_state = RolloverWindowState()
        self._rollover_suggestion: list[str] | None = None

        suggestion_bar, self.rollover_banner_label = self._rollover_bar()
        create = Gtk.Button(label="Create Rollover Group")
        create.set_valign(Gtk.Align.CENTER)
        create.connect("clicked", self._on_rollover_banner_clicked)
        suggestion_bar.append(create)
        self.rollover_banner = self._rollover_revealer(suggestion_bar)

        selection_bar, self.rollover_selection_label = self._rollover_bar()
        cancel = Gtk.Button(label="Cancel")
        cancel.set_valign(Gtk.Align.CENTER)
        cancel.connect("clicked", self._on_rollover_selection_cancel_clicked)
        selection_bar.append(cancel)
        self.rollover_finish_button = Gtk.Button()
        self.rollover_finish_button.set_valign(Gtk.Align.CENTER)
        self.rollover_finish_button.add_css_class("suggested-action")
        self.rollover_finish_button.connect("clicked", self._on_rollover_selection_finish_clicked)
        selection_bar.append(self.rollover_finish_button)
        self.rollover_selection_revealer = self._rollover_revealer(selection_bar)

        if not getattr(self, "_rollover_signals_connected", False):
            self._rollover_signals_connected = True
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self._on_rollover_key_pressed)
            self.add_controller(keys)
        self._update_rollover_selection_bar()
        self._update_rollover_banner()

    @staticmethod
    def _rollover_bar() -> tuple[Gtk.Box, Gtk.Label]:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.add_css_class("rollover-bar")
        label = Gtk.Label(xalign=0.0, hexpand=True)
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_width_chars(20)
        bar.append(label)
        return bar, label

    def _rollover_revealer(self: Any, child: Gtk.Widget) -> Gtk.Revealer:
        revealer = Gtk.Revealer()
        revealer.set_child(child)
        revealer.set_reveal_child(False)
        # A collapsed revealer still takes the tab's spacing, so hide it.
        revealer.set_visible(False)
        revealer.connect("notify::child-revealed", _hide_collapsed_revealer)
        self.append(revealer)
        return revealer

    # Window-wide state

    def _rollover_ui_state(self: Any) -> RolloverWindowState:
        state = getattr(self.main_window, "rollover_state", None)
        if isinstance(state, RolloverWindowState):
            return state
        return self._local_rollover_state

    @property
    def _rollover_selection(self: Any) -> RolloverSelection | None:
        return self._rollover_ui_state().selection

    def _rollover_tabs(self: Any) -> list[Any]:
        """Every open device tab of the window, including this one."""
        root = self.main_window
        if root is None or not hasattr(root, "tab_view"):
            return [self]
        from keymasq.gui.window.tab_layout import _iter_profile_tabs

        tabs = [child for child in _iter_profile_tabs(root) if isinstance(child, RolloverMixin)]
        return tabs if self in tabs else [self, *tabs]

    def _rollover_tab_for(self: Any, hardware_id: str) -> Any | None:
        return next(
            (tab for tab in self._rollover_tabs() if tab.device.hardware_id == hardware_id),
            None,
        )

    def _present_rollover_tab(self: Any) -> None:
        root = self.main_window
        if root is None or not hasattr(root, "tab_view"):
            return
        from keymasq.gui.window.tab_layout import _page_for_child

        page = _page_for_child(root, self)
        if page is not None:
            root.tab_view.set_selected_page(page)

    # Group lookup and persistence

    def _rollover_member(self: Any, button_id: str) -> RolloverMember:
        return RolloverMember(hardware_id=self.device.hardware_id, button=button_id)

    def _rollover_groups(self: Any, profile: ProfileInfo | None = None) -> list[RolloverGroup]:
        profile = profile or self._selected_profile
        return list(profile.config.rollover_groups) if profile is not None else []

    def _rollover_group_for_button(self: Any, button_id: str) -> RolloverGroup | None:
        return rollover_group_for_member(self._rollover_groups(), self._rollover_member(button_id))

    def _button_by_id(self: Any, button_id: str) -> ButtonDefinition | None:
        return next((button for button in self.device.buttons if button.id == button_id), None)

    def _button_label(self: Any, button_id: str) -> str:
        button = self._button_by_id(button_id)
        return button.label if button is not None else button_id

    def _rollover_member_labels(self: Any, member: RolloverMember) -> tuple[str, str]:
        """Return the member's button label and device name."""
        tab = self._rollover_tab_for(member.hardware_id)
        if tab is not None:
            return tab._button_label(member.button), tab.device.name
        hardware_manager = getattr(self, "hardware_manager", None)
        hardware = (
            hardware_manager.get_hardware(member.hardware_id)
            if hardware_manager is not None
            else None
        )
        if hardware is None:
            return member.button, member.hardware_id
        button = next((button for button in hardware.buttons if button.id == member.button), None)
        return (button.label if button is not None else member.button), hardware.name

    def _selected_member_mapping(self: Any, member: RolloverMember) -> MappingAction | None:
        profile = self._selected_profile
        layer = profile.config.get_layer(member.hardware_id) if profile is not None else None
        return layer.mappings.get(member.button) if layer is not None else None

    def _store_rollover_groups(
        self: Any,
        update: Callable[[list[RolloverGroup]], list[RolloverGroup]],
    ) -> bool:
        profile = self._resolve_mapping_target_profile(self._selected_profile)
        if profile is None:
            return False
        profile.config.rollover_groups = update(list(profile.config.rollover_groups))
        if self._profile_is_selected(profile):
            self._selected_profile = profile
        saved = bool(self._save_specific_profile(profile))
        self._refresh_rollover_presentation()
        return saved

    def _replace_rollover_group(self: Any, anchor: RolloverMember, group: RolloverGroup) -> bool:
        """Replace the group containing ``anchor``, or add ``group`` if none does."""

        def update(groups: list[RolloverGroup]) -> list[RolloverGroup]:
            index = next(
                (i for i, existing in enumerate(groups) if anchor in existing.members),
                None,
            )
            if index is None:
                return [*groups, group]
            groups[index] = group
            return groups

        return self._store_rollover_groups(update)

    def _delete_rollover_group(self: Any, anchor: RolloverMember) -> None:
        def update(groups: list[RolloverGroup]) -> list[RolloverGroup]:
            return [group for group in groups if anchor not in group.members]

        self._store_rollover_groups(update)

    def _refresh_rollover_presentation(self: Any) -> None:
        """Repaint rollover state in every device tab and in the open editor."""
        for tab in self._rollover_tabs():
            tab._repaint_rollover()
        self._refresh_rollover_dialog()

    def _repaint_rollover(self: Any) -> None:
        for button_id in self._button_widgets:
            self._update_button_display(button_id)
        self._update_header_caption()
        self._update_rollover_selection_bar()
        self._update_rollover_banner()

    def _refresh_rollover_dialog(self: Any) -> None:
        state = self._rollover_ui_state()
        if state.dialog is None or state.dialog_anchor is None:
            return
        current = rollover_group_for_member(self._rollover_groups(), state.dialog_anchor)
        if current is not None:
            state.dialog.refresh(current)

    # Selection mode

    def _on_rollover_group_clicked(self: Any, _button: Gtk.Button) -> None:
        self._start_rollover_selection()

    def _start_rollover_selection(
        self: Any,
        preselected: list[RolloverMember] | None = None,
        editing: RolloverGroup | None = None,
    ) -> None:
        if self._selected_profile is None:
            self._show_no_profile_dialog()
            return
        self._rollover_ui_state().selection = RolloverSelection(
            profile_name=self._selected_profile.config.name,
            selected=list(preselected or (editing.members if editing else [])),
            editing_member=editing.members[0] if editing else None,
        )
        self._update_rollover_selection_everywhere()

    def _editing_rollover_group(self: Any) -> RolloverGroup | None:
        selection = self._rollover_selection
        if selection is None or selection.editing_member is None:
            return None
        return rollover_group_for_member(self._rollover_groups(), selection.editing_member)

    def _rollover_selection_block_reason(self: Any, button_id: str) -> str | None:
        return selection_block_reason(
            self._rollover_member(button_id),
            self._rollover_groups(),
            self._editing_rollover_group(),
        )

    def _toggle_rollover_member(self: Any, button_id: str) -> None:
        selection = self._rollover_selection
        if selection is None or self._rollover_selection_block_reason(button_id) is not None:
            return
        selection.toggle(self._rollover_member(button_id))
        self._update_rollover_selection_everywhere()

    def _update_rollover_selection_everywhere(self: Any) -> None:
        for tab in self._rollover_tabs():
            tab._update_rollover_selection_ui()

    def _update_rollover_selection_ui(self: Any) -> None:
        self._update_rollover_selection_bar()
        self._update_rollover_banner()
        for button_id in self._button_widgets:
            self._update_rollover_cell_state(button_id)

    def _update_rollover_selection_bar(self: Any) -> None:
        revealer = getattr(self, "rollover_selection_revealer", None)
        if revealer is None:
            return
        selection = self._rollover_selection
        _set_revealed(revealer, selection is not None)
        if selection is None:
            return
        self.rollover_selection_label.set_text(
            selection_summary(
                self._rollover_selection_devices(selection.selected),
                self.device.name,
            )
        )
        editing = self._editing_rollover_group()
        self.rollover_finish_button.set_label("Save" if editing is not None else "Create")
        self.rollover_finish_button.set_sensitive(selection.can_finish)

    def _rollover_selection_devices(
        self: Any,
        members: list[RolloverMember],
    ) -> list[tuple[str, list[str]]]:
        by_device: dict[str, tuple[str, list[str]]] = {}
        for member in members:
            label, device = self._rollover_member_labels(member)
            by_device.setdefault(member.hardware_id, (device, []))[1].append(label)
        return list(by_device.values())

    def _update_rollover_cell_state(self: Any, button_id: str) -> None:
        widget = self._button_widgets.get(button_id)
        if widget is None:
            return
        selection = self._rollover_selection
        widget.remove_css_class(_SELECTED_CLASS)
        widget.remove_css_class(_UNAVAILABLE_CLASS)
        if selection is None:
            return
        is_button = self._button_by_id(button_id) is not None
        reason = (
            self._rollover_selection_block_reason(button_id)
            if is_button
            else "Only keys and buttons can be used in a rollover group."
        )
        if reason is not None:
            widget.add_css_class(_UNAVAILABLE_CLASS)
            widget.set_tooltip_text(reason)
        elif self._rollover_member(button_id) in selection.selected:
            widget.add_css_class(_SELECTED_CLASS)

    def _end_rollover_selection(self: Any) -> None:
        state = self._rollover_ui_state()
        if state.selection is None:
            return
        state.selection = None
        for tab in self._rollover_tabs():
            tab._repaint_rollover()

    def _sync_rollover_selection_with_profile(self: Any) -> None:
        """Keep selection mode across profile refreshes, but not across a profile switch."""
        selection = self._rollover_selection
        if selection is None:
            return
        profile = self._selected_profile
        if profile is None or profile.config.name != selection.profile_name:
            self._end_rollover_selection()
        else:
            self._update_rollover_selection_ui()

    def _on_rollover_selection_cancel_clicked(self: Any, _button: Gtk.Button) -> None:
        self._end_rollover_selection()

    def _on_rollover_selection_finish_clicked(self: Any, _button: Gtk.Button) -> None:
        self._finish_rollover_selection()

    def _finish_rollover_selection(self: Any) -> None:
        selection = self._rollover_selection
        if selection is None or not selection.can_finish:
            return
        members = list(selection.selected)
        editing = self._editing_rollover_group()
        self._end_rollover_selection()
        if editing is not None:
            group = replace(editing, members=members)
            anchor = editing.members[0]
        else:
            labels = {member: self._rollover_member_labels(member)[0] for member in members}
            group = RolloverGroup(
                name=default_rollover_group_name(members, labels),
                members=members,
            )
            anchor = members[0]
        if self._replace_rollover_group(anchor, group):
            self._open_rollover_group_editor(group)

    def _on_rollover_key_pressed(
        self: Any,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval == Gdk.KEY_Escape and self._rollover_selection is not None:
            self._end_rollover_selection()
            return True
        return False

    # Cell activation

    def _handle_rollover_cell_activation(self: Any, button: ButtonDefinition) -> bool:
        """Route a cell click in selection mode or on a member. Returns True if handled."""
        if self._rollover_selection is not None:
            self._toggle_rollover_member(button.id)
            return True
        group = self._rollover_group_for_button(button.id)
        if group is None:
            return False
        self._open_rollover_group_editor(group)
        return True

    # Editor

    def _open_rollover_group_editor(self: Any, group: RolloverGroup) -> None:
        state = self._rollover_ui_state()
        if state.dialog is not None:
            state.dialog.close()
        state.dialog_anchor = group.members[0]

        def anchor() -> RolloverMember:
            return state.dialog_anchor or group.members[0]

        def save(updated: RolloverGroup) -> bool:
            saved = self._replace_rollover_group(anchor(), updated)
            if saved and updated.members:
                state.dialog_anchor = updated.members[0]
            return saved

        def delete() -> None:
            self._delete_rollover_group(anchor())

        def change_members() -> None:
            current = rollover_group_for_member(self._rollover_groups(), anchor())
            if current is not None:
                self._start_rollover_selection(editing=current)

        dialog = RolloverGroupDialog(
            group,
            RolloverDialogCallbacks(
                describe_member=self._describe_rollover_member,
                save=save,
                delete=delete,
                change_members=change_members,
                edit_mapping=self._edit_rollover_member_mapping,
            ),
        )
        state.dialog = dialog
        dialog.connect("closed", self._on_rollover_dialog_closed)
        dialog.present(self.get_root())

    def _on_rollover_dialog_closed(self: Any, dialog: RolloverGroupDialog) -> None:
        state = self._rollover_ui_state()
        if state.dialog is dialog:
            state.dialog = None
            state.dialog_anchor = None

    def _edit_rollover_member_mapping(self: Any, member: RolloverMember) -> None:
        """Open the key selector for a member, in its own device tab."""
        tab = self._rollover_tab_for(member.hardware_id)
        button = tab._button_by_id(member.button) if tab is not None else None
        if tab is None or button is None:
            return
        if tab is not self:
            tab._present_rollover_tab()
        tab._show_function_editor(button)

    def _describe_rollover_member(self: Any, member: RolloverMember) -> RolloverMemberInfo:
        label, device = self._rollover_member_labels(member)
        mapping = self._selected_member_mapping(member)
        tab = self._rollover_tab_for(member.hardware_id)
        button = tab._button_by_id(member.button) if tab is not None else None
        if mapping is None:
            description = "No mapping in this profile"
            # An unmapped keyboard key passes through as itself.
            sends_keys = (
                tab is not None
                and button is not None
                and button.evdev.lower().startswith("key_")
                and tab._default_output_description(button) is None
            )
        else:
            describer = tab if tab is not None else self
            description = describer._describe_mapping(mapping, button)
            sends_keys = mapping.action_type is ActionType.KEYBOARD
        return RolloverMemberInfo(
            label=label,
            device=device,
            mapping=description,
            sends_keys=sends_keys,
        )

    # Suggestion banner

    def _update_rollover_banner(self: Any) -> None:
        banner = getattr(self, "rollover_banner", None)
        if banner is None:
            return
        layer = self._selected_layer()
        suggestion = None
        if layer is not None and self._rollover_selection is None and not self.demo_mode:
            hardware_id = self.device.hardware_id
            grouped = {
                member.button
                for group in self._rollover_groups()
                for member in group.members
                if member.hardware_id == hardware_id
            }
            suggestion = suggest_axis_rollover_members(
                layer.mappings,
                grouped,
                [button.id for button in self.device.buttons],
            )
        self._rollover_suggestion = suggestion
        if suggestion is None or layer is None:
            _set_revealed(banner, False)
            return
        axis = axis_display_name(layer.mappings[suggestion[0]].target)
        names = [self._button_label(member) for member in suggestion]
        joined = " and ".join(names) if len(names) == 2 else ", ".join(names)
        verb = "both drive" if len(names) == 2 else "all drive"
        self.rollover_banner_label.set_text(
            f"{joined} {verb} {axis}. Releasing one centers the axis "
            "even while another is held."
        )
        _set_revealed(banner, True)

    def _on_rollover_banner_clicked(self: Any, _button: Gtk.Button) -> None:
        suggestion = self._rollover_suggestion
        if suggestion:
            self._start_rollover_selection(
                preselected=[self._rollover_member(button_id) for button_id in suggestion]
            )


def _set_revealed(revealer: Gtk.Revealer, revealed: bool) -> None:
    if revealed:
        revealer.set_visible(True)
    revealer.set_reveal_child(revealed)
    _hide_collapsed_revealer(revealer, None)


def _hide_collapsed_revealer(revealer: Gtk.Revealer, _param: object) -> None:
    if not revealer.get_reveal_child() and not revealer.get_child_revealed():
        revealer.set_visible(False)
