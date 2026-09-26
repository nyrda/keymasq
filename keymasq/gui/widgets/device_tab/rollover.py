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
from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
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
    rollover_group_color_class,
    selection_block_reason,
    selection_summary,
    suggest_axis_rollover_members,
)
from keymasq.session.profile.types import ProfileInfo
from keymasq.session.superkeys import SuperkeyManager

_SELECTED_CLASS = "rollover-selected"
_UNAVAILABLE_CLASS = "rollover-unavailable"
# Members with these actions can press keys, so the editor shows the
# anti-cheat notice for them.
_KEY_SENDING_ACTIONS = frozenset(
    {
        ActionType.KEYBOARD,
        ActionType.SUPERKEY,
        ActionType.MACRO,
        ActionType.PLAY_MACRO_SLOT,
        ActionType.REPEAT,
    }
)


@dataclass
class RolloverWindowState:
    """Rollover UI state shared by every device tab of one window."""

    selection: RolloverSelection | None = None
    dialog: RolloverGroupDialog | None = None
    # A member of the group the open editor shows. Saving can change the
    # group's name and members, so the editor finds its group through this.
    dialog_anchor: RolloverMember | None = None
    # The profile the open editor's group belongs to.
    dialog_profile: str | None = None


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

    def _rollover_group_color_class(self: Any, group: RolloverGroup) -> str:
        return rollover_group_color_class(self._rollover_groups(), group)

    def _button_by_id(self: Any, button_id: str) -> ButtonDefinition | None:
        return next((button for button in self.device.buttons if button.id == button_id), None)

    def _button_label(self: Any, button_id: str) -> str:
        button = self._button_by_id(button_id)
        return button.label if button is not None else button_id

    def _rollover_member_hardware(self: Any, member: RolloverMember) -> HardwareConfig | None:
        """Return the member's device from its open tab, or from saved hardware."""
        tab = self._rollover_tab_for(member.hardware_id)
        if tab is not None:
            return tab.device
        hardware_manager = getattr(self, "hardware_manager", None)
        if hardware_manager is None:
            return None
        return hardware_manager.get_hardware(member.hardware_id)

    def _rollover_member_button(self: Any, member: RolloverMember) -> ButtonDefinition | None:
        hardware = self._rollover_member_hardware(member)
        if hardware is None:
            return None
        return next((button for button in hardware.buttons if button.id == member.button), None)

    def _rollover_member_labels(self: Any, member: RolloverMember) -> tuple[str, str]:
        """Return the member's button label and device name."""
        hardware = self._rollover_member_hardware(member)
        if hardware is None:
            return member.button, member.hardware_id
        button = self._rollover_member_button(member)
        return (button.label if button is not None else member.button), hardware.name

    def _selected_member_mapping(self: Any, member: RolloverMember) -> MappingAction | None:
        profile = self._selected_profile
        layer = profile.config.get_layer(member.hardware_id) if profile is not None else None
        return layer.mappings.get(member.button) if layer is not None else None

    def _store_rollover_groups(
        self: Any,
        update: Callable[[list[RolloverGroup]], list[RolloverGroup] | None],
    ) -> bool:
        """Save the groups ``update`` returns. ``None`` leaves the profile unchanged."""
        profile = self._resolve_mapping_target_profile(self._selected_profile)
        if profile is None:
            return False
        groups = update(list(profile.config.rollover_groups))
        if groups is None:
            return False
        profile.config.rollover_groups = groups
        if self._profile_is_selected(profile):
            self._selected_profile = profile
        saved = bool(self._save_specific_profile(profile))
        self._refresh_rollover_presentation()
        return saved

    def _add_rollover_group(self: Any, group: RolloverGroup) -> bool:
        def update(groups: list[RolloverGroup]) -> list[RolloverGroup]:
            return [*groups, group]

        return self._store_rollover_groups(update)

    def _replace_rollover_group(self: Any, anchor: RolloverMember, group: RolloverGroup) -> bool:
        """Replace the group containing ``anchor``. Fails if no group contains it."""

        def update(groups: list[RolloverGroup]) -> list[RolloverGroup] | None:
            index = next(
                (i for i, existing in enumerate(groups) if anchor in existing.members),
                None,
            )
            if index is None:
                return None
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
        self._sync_rollover_dialog()

    def _repaint_rollover(self: Any) -> None:
        for button_id in self._button_widgets:
            self._update_button_display(button_id)
        self._update_header_caption()
        self._update_rollover_selection_bar()
        self._update_rollover_banner()

    def _dialog_rollover_group(self: Any) -> RolloverGroup | None:
        """Return the open editor's group, or None if it is gone from the selected profile."""
        state = self._rollover_ui_state()
        profile = self._selected_profile
        if (
            state.dialog_anchor is None
            or profile is None
            or profile.config.name != state.dialog_profile
        ):
            return None
        return rollover_group_for_member(self._rollover_groups(profile), state.dialog_anchor)

    def _sync_rollover_dialog(self: Any) -> None:
        """Show the current group in the open editor, or close it if the group is gone.

        The editor closes when the selected profile changes or a profile
        reload removed its group, so it never saves into the wrong profile.
        """
        state = self._rollover_ui_state()
        dialog = state.dialog
        if dialog is None:
            return
        current = self._dialog_rollover_group()
        if current is not None:
            dialog.refresh(current)
            return
        self._forget_rollover_dialog(dialog)
        dialog.close()

    def _forget_rollover_dialog(self: Any, dialog: RolloverGroupDialog) -> None:
        state = self._rollover_ui_state()
        if state.dialog is dialog:
            state.dialog = None
            state.dialog_anchor = None
            state.dialog_profile = None

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
        button = self._button_by_id(button_id)
        if button is None:
            return "Only keys and buttons can be used in a rollover group."
        return selection_block_reason(
            self._rollover_member(button_id),
            self._rollover_groups(),
            self._editing_rollover_group(),
            evdev=button.evdev,
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
        reason = self._rollover_selection_block_reason(button_id)
        if reason is not None or self._rollover_group_for_button(button_id) is not None:
            # The action label's own tooltip would hide the reason, and a
            # member's "Click to edit the group" is wrong while selecting.
            # Ending selection mode repaints the cell and restores it.
            action_label = getattr(widget, "_action_label", None)
            if action_label is not None:
                action_label.set_tooltip_text(None)
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
        if (
            profile is None
            or profile.config.name != selection.profile_name
            or self._edited_rollover_group_is_gone()
        ):
            self._end_rollover_selection()
        else:
            self._update_rollover_selection_ui()

    def _edited_rollover_group_is_gone(self: Any) -> bool:
        selection = self._rollover_selection
        return (
            selection is not None
            and selection.editing_member is not None
            and self._editing_rollover_group() is None
        )

    def _prune_rollover_selection(self: Any) -> None:
        """Drop selected keys whose device or control was deleted."""
        selection = self._rollover_selection
        if selection is None:
            return
        editing = self._editing_rollover_group()
        groups = self._rollover_groups()
        # A reload can also put a selected key into another group meanwhile.
        kept = [
            member
            for member in selection.selected
            if self._rollover_member_exists(member)
            and rollover_group_for_member(groups, member) in (None, editing)
        ]
        selection.selected = kept
        if editing is not None and selection.editing_member not in kept:
            # Keep finding the edited group through a member that still exists.
            selection.editing_member = next(
                (member for member in editing.members if member in kept),
                selection.editing_member,
            )

    def _rollover_member_exists(self: Any, member: RolloverMember) -> bool:
        return self._rollover_member_button(member) is not None

    def _on_rollover_device_removed(self: Any) -> None:
        """Called on a remaining tab after a device tab was removed."""
        self._prune_rollover_selection()
        self._update_rollover_selection_everywhere()

    def _on_rollover_selection_cancel_clicked(self: Any, _button: Gtk.Button) -> None:
        self._end_rollover_selection()

    def _on_rollover_selection_finish_clicked(self: Any, _button: Gtk.Button) -> None:
        self._finish_rollover_selection()

    def _finish_rollover_selection(self: Any) -> None:
        self._prune_rollover_selection()
        selection = self._rollover_selection
        if selection is None or not selection.can_finish:
            self._update_rollover_selection_everywhere()
            return
        members = list(selection.selected)
        editing = self._editing_rollover_group()
        gone = self._edited_rollover_group_is_gone()
        self._end_rollover_selection()
        if gone:
            # Change Members must not bring back a group removed meanwhile.
            return
        if editing is not None:
            group = replace(editing, members=members)
            saved = self._replace_rollover_group(editing.members[0], group)
        else:
            labels = {member: self._rollover_member_labels(member)[0] for member in members}
            group = RolloverGroup(
                name=default_rollover_group_name(members, labels),
                members=members,
            )
            saved = self._add_rollover_group(group)
        if saved:
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
            previous = state.dialog
            self._forget_rollover_dialog(previous)
            previous.close()
        profile = self._selected_profile
        state.dialog_anchor = group.members[0]
        state.dialog_profile = profile.config.name if profile is not None else None

        def anchor() -> RolloverMember:
            return state.dialog_anchor or group.members[0]

        def save(updated: RolloverGroup) -> bool:
            # A profile switch or reload can remove the group while the
            # editor is open. Saving then must not add it back.
            if state.dialog is not dialog or self._dialog_rollover_group() is None:
                self._sync_rollover_dialog()
                return False
            previous_anchor = anchor()
            # Saving repaints the editor, which finds its group through the
            # anchor, and the saved group may no longer contain the old one.
            if updated.members:
                state.dialog_anchor = updated.members[0]
            saved = self._replace_rollover_group(previous_anchor, updated)
            if not saved:
                if state.dialog is dialog:
                    state.dialog_anchor = previous_anchor
                self._sync_rollover_dialog()
            return saved

        def delete() -> None:
            target = anchor()
            self._forget_rollover_dialog(dialog)
            self._delete_rollover_group(target)

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
        self._forget_rollover_dialog(dialog)

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
        button = self._rollover_member_button(member)
        if mapping is None:
            description = "No mapping in this profile"
        else:
            describer = tab if tab is not None else self
            description = describer._describe_mapping(mapping, button)
        effective = mapping
        if effective is None and tab is not None:
            # Without a mapping here, a lower profile's mapping still applies.
            _profile_name, effective = tab._get_effective_mapping_for_button(member.button)
        if effective is None or effective.action_type is ActionType.PASSTHROUGH:
            # A keyboard key passes through as itself unless the device routes
            # it to a default output. Without an open tab, assume it doesn't.
            sends_keys = (
                button is not None
                and button.evdev.lower().startswith("key_")
                and (tab is None or tab._default_output_description(button) is None)
            )
        else:
            sends_keys = _sends_keys(effective)
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
                self.device.buttons,
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


def _sends_keys(action: MappingAction) -> bool:
    """Whether an action can send keyboard keys.

    Macro bodies are not readable here, so macros count as sending keys.
    """
    if action.action_type is not ActionType.SUPERKEY:
        return action.action_type in _KEY_SENDING_ACTIONS
    config = action.superkey_config or (
        SuperkeyManager().get_superkey(action.superkey_name) if action.superkey_name else None
    )
    if config is None:
        return True
    return any(
        child.action_type in _KEY_SENDING_ACTIONS
        for child in (
            *config.tap_actions,
            *config.double_tap_actions,
            *config.hold_actions,
            *config.tap_hold_actions,
            *config.overload_actions,
            *config.overload_down_actions,
            *config.overload_up_actions,
        )
    )
