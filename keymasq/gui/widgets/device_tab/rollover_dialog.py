"""Editor for one rollover group in a device tab."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq import __version__
from keymasq.common.model.profiles import RolloverGroup, RolloverMember, RolloverWinner
from keymasq.common.rollover import ROLLOVER_MIN_MEMBERS
from keymasq.gui.preferences import (
    load_rollover_anti_cheat_warning_dismissed,
    save_rollover_anti_cheat_warning_dismissed,
)
from keymasq.gui.widgets.device_tab.rollover_state import (
    ROLLOVER_RESTORE_SUBTITLE,
    ROLLOVER_RESTORE_TITLE,
    ROLLOVER_WINNER_CHOICES,
)
from keymasq.gui.widgets.docs_links import docs_page_url

log = logging.getLogger(__name__)


def rollover_docs_url(anchor: str | None = None) -> str:
    return docs_page_url("rollover", anchor=anchor, version=__version__)


@dataclass(frozen=True)
class RolloverMemberInfo:
    label: str
    device: str
    mapping: str
    sends_keys: bool = False


@dataclass(frozen=True)
class RolloverDialogCallbacks:
    describe_member: Callable[[RolloverMember], RolloverMemberInfo]
    save: Callable[[RolloverGroup], bool]
    delete: Callable[[], None]
    change_members: Callable[[], None]
    edit_mapping: Callable[[RolloverMember], None]


class RolloverGroupDialog(Adw.Dialog):
    def __init__(self, group: RolloverGroup, callbacks: RolloverDialogCallbacks) -> None:
        super().__init__(title="Rollover Group", content_width=480, content_height=620)
        self._group = replace(group, members=list(group.members))
        self._callbacks = callbacks
        self._member_rows: list[Adw.ActionRow] = []
        self._anti_cheat_warning_dismissed = load_rollover_anti_cheat_warning_dismissed()

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()

        settings = Adw.PreferencesGroup()
        self.name_row = Adw.EntryRow(title="Name")
        self.name_row.set_text(self._group.name)
        self.name_row.set_show_apply_button(True)
        self.name_row.connect("apply", self._on_name_applied)
        settings.add(self.name_row)

        self.winner_row = Adw.ComboRow(title="Winner")
        self.winner_row.set_model(
            Gtk.StringList.new([label for _, label, _ in ROLLOVER_WINNER_CHOICES])
        )
        self.winner_row.set_selected(self._winner_index(self._group.winner))
        self.winner_row.set_subtitle(self._winner_description(self._group.winner))
        self.winner_row.connect("notify::selected", self._on_winner_selected)
        settings.add(self.winner_row)

        self.restore_row = Adw.SwitchRow(
            title=ROLLOVER_RESTORE_TITLE,
            subtitle=ROLLOVER_RESTORE_SUBTITLE,
        )
        self.restore_row.set_active(self._group.restore)
        self.restore_row.connect("notify::active", self._on_restore_toggled)
        settings.add(self.restore_row)
        page.add(settings)

        self.members_group = Adw.PreferencesGroup(
            title="Members",
            description=(
                "Only one of these keys drives its mapping at a time. "
                "Keys can be on different devices."
            ),
        )
        self.change_members_button = Gtk.Button(label="Change Members")
        self.change_members_button.add_css_class("flat")
        self.change_members_button.set_valign(Gtk.Align.CENTER)
        self.change_members_button.connect("clicked", self._on_change_members_clicked)
        self.members_group.set_header_suffix(self.change_members_button)
        page.add(self.members_group)

        danger = Adw.PreferencesGroup()
        self.delete_button = Gtk.Button(label="Delete Group")
        self.delete_button.add_css_class("destructive-action")
        self.delete_button.set_halign(Gtk.Align.START)
        self.delete_button.connect("clicked", self._on_delete_clicked)
        danger.add(self.delete_button)
        page.add(danger)

        toolbar.set_content(page)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_margin_top(6)
        footer.set_margin_bottom(6)
        footer.set_margin_start(12)
        footer.set_margin_end(12)
        self.docs_button = Gtk.Button(label="?")
        self.docs_button.add_css_class("flat")
        self.docs_button.add_css_class("actions-docs-button")
        self.docs_button.set_tooltip_text("Open Rollover Groups documentation")
        self.docs_button.connect("clicked", self._on_docs_clicked)
        self.docs_button.set_valign(Gtk.Align.CENTER)
        footer.append(self.docs_button)
        self.anti_cheat_note = self._anti_cheat_note()
        footer.append(self.anti_cheat_note)
        toolbar.add_bottom_bar(footer)
        self.set_child(toolbar)
        self.refresh()

    def _anti_cheat_note(self) -> Gtk.Box:
        note = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        note.set_hexpand(True)
        note.set_visible(False)
        icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        icon.add_css_class("dim-label")
        note.append(icon)
        label = Gtk.Label()
        label.set_markup(
            "Some games, like Counter-Strike 2, kick or ban players for automatic "
            'key handover. <a href="anti-cheat">Learn more</a>'
        )
        label.add_css_class("caption")
        label.add_css_class("dim-label")
        label.set_wrap(True)
        label.set_xalign(0)
        label.set_hexpand(True)
        label.connect("activate-link", self._on_anti_cheat_link)
        note.append(label)
        dismiss = self._icon_button("window-close-symbolic", "Don't show this warning again")
        dismiss.connect("clicked", self._on_anti_cheat_dismissed)
        note.append(dismiss)
        return note

    @property
    def group(self) -> RolloverGroup:
        return self._group

    def refresh(self, group: RolloverGroup | None = None) -> None:
        """Rebuild member rows, for example after a member's mapping changed."""
        if group is not None:
            self._group = replace(group, members=list(group.members))
        for row in self._member_rows:
            self.members_group.remove(row)
        self._member_rows = []
        sends_keys = False
        priority = self._group.winner is RolloverWinner.PRIORITY
        last = len(self._group.members) - 1
        for index, member in enumerate(self._group.members):
            info = self._callbacks.describe_member(member)
            sends_keys = sends_keys or info.sends_keys
            row = self._member_row(member, info, index=index, priority=priority, last=last)
            self.members_group.add(row)
            self._member_rows.append(row)
        self.anti_cheat_note.set_visible(
            sends_keys and not self._anti_cheat_warning_dismissed
        )

    def _member_row(
        self,
        member: RolloverMember,
        info: RolloverMemberInfo,
        *,
        index: int,
        priority: bool,
        last: int,
    ) -> Adw.ActionRow:
        row = Adw.ActionRow(title=info.label, subtitle=f"{info.device} · {info.mapping}")
        if priority:
            up = self._icon_button("go-up-symbolic", "Move up")
            up.set_sensitive(index > 0)
            up.connect("clicked", self._on_move_clicked, member, -1)
            row.add_suffix(up)
            down = self._icon_button("go-down-symbolic", "Move down")
            down.set_sensitive(index < last)
            down.connect("clicked", self._on_move_clicked, member, 1)
            row.add_suffix(down)
        edit = self._icon_button("document-edit-symbolic", "Edit mapping")
        edit.connect("clicked", self._on_edit_mapping_clicked, member)
        row.add_suffix(edit)
        remove = self._icon_button("list-remove-symbolic", "Remove from group")
        remove.connect("clicked", self._on_remove_clicked, member)
        row.add_suffix(remove)
        return row

    @staticmethod
    def _icon_button(icon_name: str, tooltip: str) -> Gtk.Button:
        button = Gtk.Button(icon_name=icon_name)
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(tooltip)
        return button

    @staticmethod
    def _winner_index(winner: RolloverWinner) -> int:
        return next(
            index
            for index, (choice, _, _) in enumerate(ROLLOVER_WINNER_CHOICES)
            if choice is winner
        )

    @staticmethod
    def _winner_description(winner: RolloverWinner) -> str:
        return next(text for choice, _, text in ROLLOVER_WINNER_CHOICES if choice is winner)

    def _save(self, group: RolloverGroup) -> None:
        if self._callbacks.save(group):
            self._group = group
            self.refresh()

    def _on_name_applied(self, row: Adw.EntryRow) -> None:
        name = row.get_text().strip()
        if not name:
            row.set_text(self._group.name)
            return
        self._save(replace(self._group, name=name))

    def _on_winner_selected(self, row: Adw.ComboRow, _param: object) -> None:
        index = int(row.get_selected())
        if not 0 <= index < len(ROLLOVER_WINNER_CHOICES):
            return
        winner = ROLLOVER_WINNER_CHOICES[index][0]
        row.set_subtitle(self._winner_description(winner))
        if winner is not self._group.winner:
            self._save(replace(self._group, winner=winner))

    def _on_restore_toggled(self, row: Adw.SwitchRow, _param: object) -> None:
        restore = bool(row.get_active())
        if restore != self._group.restore:
            self._save(replace(self._group, restore=restore))

    def _on_move_clicked(self, _button: Gtk.Button, member: RolloverMember, offset: int) -> None:
        members = list(self._group.members)
        index = members.index(member)
        target = index + offset
        if not 0 <= target < len(members):
            return
        members[index], members[target] = members[target], members[index]
        self._save(replace(self._group, members=members))

    def _on_edit_mapping_clicked(self, _button: Gtk.Button, member: RolloverMember) -> None:
        self._callbacks.edit_mapping(member)

    def _on_remove_clicked(self, _button: Gtk.Button, member: RolloverMember) -> None:
        members = [candidate for candidate in self._group.members if candidate != member]
        if len(members) >= ROLLOVER_MIN_MEMBERS:
            self._save(replace(self._group, members=members))
            return
        self._confirm_delete(
            f"A rollover group needs at least {ROLLOVER_MIN_MEMBERS} keys. "
            "Removing this key deletes the group."
        )

    def _on_docs_clicked(self, _button: Gtk.Button) -> None:
        self._open_documentation(rollover_docs_url())

    def _on_anti_cheat_link(self, _label: Gtk.Label, anchor: str) -> bool:
        self._open_documentation(rollover_docs_url(anchor))
        return True

    def _on_anti_cheat_dismissed(self, _button: Gtk.Button) -> None:
        self._anti_cheat_warning_dismissed = True
        self.anti_cheat_note.set_visible(False)
        save_rollover_anti_cheat_warning_dismissed()

    def _open_documentation(self, url: str) -> None:
        try:
            Gtk.UriLauncher.new(url).launch(None, None, None)
        except Exception:
            log.exception("Could not open rollover group documentation %s", url)

    def _on_change_members_clicked(self, _button: Gtk.Button) -> None:
        self.close()
        self._callbacks.change_members()

    def _on_delete_clicked(self, _button: Gtk.Button) -> None:
        self._confirm_delete("The keys go back to their own mappings.")

    def _confirm_delete(self, body: str) -> None:
        alert = Adw.AlertDialog(heading="Delete Rollover Group?", body=body)
        alert.add_response("cancel", "Cancel")
        alert.add_response("delete", "Delete")
        alert.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")
        alert.connect("response", self._on_delete_response)
        alert.present(self)

    def _on_delete_response(self, _alert: Adw.AlertDialog, response: str) -> None:
        if response != "delete":
            return
        self._callbacks.delete()
        self.close()
