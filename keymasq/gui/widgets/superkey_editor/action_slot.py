"""Typed action-slot component used by the Super Key editor."""

from collections.abc import Callable, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.superkeys import SuperkeyAction


class ActionSlot[ActionT: SuperkeyAction | MappingAction]:
    """Own one action bundle and its expander-row presentation."""

    def __init__(
        self,
        *,
        title: str,
        action_key: str,
        describe_action: Callable[[ActionT], str],
        edit_requested: Callable[["ActionSlot[ActionT]"], None],
        modified: Callable[[], None],
        static_description: str | None = None,
        tooltip: str | None = None,
    ) -> None:
        self.action_key = action_key
        self._describe_action = describe_action
        self._edit_requested = edit_requested
        self._modified = modified
        self._static_description = static_description
        self._actions: list[ActionT] = []
        self._child_rows: list[Adw.ActionRow] = []

        self.row = Adw.ExpanderRow()
        self.row.set_title(title)
        self.row.set_subtitle("(none)")
        self.row.set_enable_expansion(False)
        if tooltip:
            self.row.set_tooltip_text(tooltip)

        edit_button = Gtk.Button(label="Edit")
        edit_button.add_css_class("flat")
        edit_button.connect("clicked", self._on_edit_clicked)
        self.row.add_suffix(edit_button)

        clear_button = Gtk.Button(icon_name="edit-clear-symbolic")
        clear_button.add_css_class("flat")
        clear_button.connect("clicked", self._on_clear_clicked)
        self.row.add_suffix(clear_button)
        self._refresh()

    @property
    def actions(self) -> list[ActionT]:
        return list(self._actions)

    @property
    def child_rows(self) -> tuple[Adw.ActionRow, ...]:
        return tuple(self._child_rows)

    def set_actions(self, actions: Sequence[ActionT], *, notify: bool = False) -> None:
        self._actions = list(actions)
        self._refresh()
        if notify:
            self._modified()

    def clear(self, *, notify: bool = True) -> None:
        self.set_actions([], notify=notify)

    def set_visible(self, visible: bool) -> None:
        self.row.set_visible(visible)

    def _on_edit_clicked(self, _button: Gtk.Button) -> None:
        self._edit_requested(self)

    def _on_clear_clicked(self, _button: Gtk.Button) -> None:
        self.clear()

    def _refresh(self) -> None:
        for child in self._child_rows:
            self.row.remove(child)
        self._child_rows.clear()

        subtitle_parts: list[str] = []
        if self._static_description:
            subtitle_parts.append(self._static_description)
        if self._actions:
            noun = "action" if len(self._actions) == 1 else "actions"
            subtitle_parts.append(f"{len(self._actions)} {noun}")
        else:
            subtitle_parts.append("(none)")
        self.row.set_subtitle("\n".join(subtitle_parts))

        if not self._actions:
            self.row.set_enable_expansion(False)
            self.row.set_expanded(False)
            return

        self.row.set_enable_expansion(True)
        self.row.set_expanded(True)
        for index, action in enumerate(self._actions, start=1):
            child = Adw.ActionRow()
            child.set_use_markup(False)
            child.set_title_lines(0)
            child.set_title(f"{index}. {self._describe_action(action)}")
            self.row.add_row(child)
            self._child_rows.append(child)
