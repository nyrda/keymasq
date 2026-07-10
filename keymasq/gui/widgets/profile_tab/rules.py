import re
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.profiles import WindowRule
from keymasq.session.profile.types import ProfileInfo


class WindowRulesMixin:
    def _on_edit_window_rules(self: Any, _button: Gtk.Button) -> None:
        if not self._selected_profile:
            return
        self._show_window_rules_dialog()

    def _show_window_rules_dialog(self: Any) -> None:
        if not self._selected_profile:
            return
        self._window_rules_target_profile_name = self._selected_profile.config.name
        self._current_rules_dialog = Adw.Dialog(
            title="Window Rules", content_width=500, content_height=450
        )
        self._current_rules_dialog.connect("closed", self._on_window_rules_dialog_closed)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(20)
        content.set_margin_bottom(20)
        content.set_margin_start(20)
        content.set_margin_end(20)

        help_label = Gtk.Label(
            label="Rules are matched with AND logic.\nUse Regex patterns for class/title/tag."
        )
        help_label.add_css_class("dim-label")
        help_label.add_css_class("caption")
        help_label.set_halign(Gtk.Align.START)
        content.append(help_label)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_margin_top(8)

        self._rules_list_box = Gtk.ListBox()
        self._rules_list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._rules_list_box.add_css_class("boxed-list")
        self._rule_rows = []

        rules = self._selected_profile.config.window_rules
        for rule in rules:
            row_widget = self._create_rule_row(rule)
            self._rules_list_box.append(row_widget)
            self._rule_rows.append(row_widget)

        if not rules:
            empty_label = self._create_empty_row()
            self._rules_list_box.append(empty_label)
            self._rule_rows.append(empty_label)
        else:
            self._update_first_rule_delete_button()

        scrolled.set_child(self._rules_list_box)
        content.append(scrolled)

        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._window_rule_capture_btn = Gtk.Button(label="Capture Window (2)")
        self._window_rule_capture_btn.connect("clicked", self._on_capture_window_rules_clicked)
        actions_box.append(self._window_rule_capture_btn)

        self._window_rule_capture_status = Gtk.Label(label="")
        self._window_rule_capture_status.add_css_class("dim-label")
        self._window_rule_capture_status.set_hexpand(True)
        self._window_rule_capture_status.set_halign(Gtk.Align.START)
        actions_box.append(self._window_rule_capture_status)

        add_btn = Gtk.Button(label="Add Rule")
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", self._on_add_window_rule)
        actions_box.append(add_btn)

        content.append(actions_box)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        remove_rules_btn = Gtk.Button(label="Remove Window Rules")
        remove_rules_btn.add_css_class("destructive-action")
        remove_rules_btn.connect("clicked", self._on_remove_window_rules_clicked)
        btn_box.append(remove_rules_btn)
        self._remove_window_rules_btn = remove_rules_btn

        btn_spacer = Gtk.Box()
        btn_spacer.set_hexpand(True)
        btn_box.append(btn_spacer)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_close_current_rules_dialog_clicked)
        btn_box.append(cancel_btn)

        apply_btn = Gtk.Button(label="Apply")
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", self._on_apply_window_rules)
        btn_box.append(apply_btn)

        content.append(btn_box)
        self._current_rules_dialog.set_child(content)
        self._update_window_rules_remove_button()
        self._current_rules_dialog.present(self.get_root())

    def _on_window_rules_dialog_closed(self: Any, dialog: Adw.Dialog) -> None:
        _ = dialog
        self._window_rules_target_profile_name = None
        self._cancel_window_rule_capture("")
        self._remove_window_rules_btn = None

    def _window_rules_target_profile(self: Any) -> ProfileInfo | None:
        if self.profile_manager is None:
            return self._selected_profile
        if self._window_rules_target_profile_name:
            return self.profile_manager.get_profile(self._window_rules_target_profile_name)
        return self._selected_profile

    def _on_capture_window_rules_clicked(self: Any, _button: Gtk.Button) -> None:
        self._cancel_window_rule_capture("")
        self._window_rule_capture_pending = True
        self._window_rule_capture_generation += 1
        self._window_rule_capture_btn.set_sensitive(False)
        self._window_rule_capture_status.set_text("Activate the target window now...")
        self._window_rule_capture_timeout_id = GLib.timeout_add(
            2000,
            self._capture_window_rules_after_delay,
        )

    def _capture_window_rules_after_delay(self: Any) -> bool:
        self._window_rule_capture_timeout_id = 0
        if not self._window_rule_capture_pending:
            return False
        self._window_rule_capture_status.set_text("Reading active window...")
        generation = self._window_rule_capture_generation

        def handle_response(response: dict | None) -> bool:
            return self._on_capture_window_rules_response(response, generation)

        self._request_session_async(
            {"command": "get_active_window"},
            handle_response,
            timeout=5.0,
        )
        return False

    def _on_capture_window_rules_response(
        self: Any,
        response: dict | None,
        generation: int,
    ) -> bool:
        if (
            not self._window_rule_capture_pending
            or generation != self._window_rule_capture_generation
        ):
            return False
        self._window_rule_capture_pending = False
        if hasattr(self, "_window_rule_capture_btn"):
            self._window_rule_capture_btn.set_sensitive(True)

        if not response or response.get("status") != "ok":
            message = (
                (response or {}).get("message") or (response or {}).get("error") or "Capture failed"
            )
            if "Unknown command: get_active_window" in message:
                message = "Please restart Keymasq Session, then try again"
            if hasattr(self, "_window_rule_capture_status"):
                self._window_rule_capture_status.set_text(message)
            return False

        rules = self._build_captured_window_rules(response)
        if not rules:
            if hasattr(self, "_window_rule_capture_status"):
                self._window_rule_capture_status.set_text("No active window details available")
            return False

        self._set_window_rule_rows(rules)
        if hasattr(self, "_window_rule_capture_status"):
            self._window_rule_capture_status.set_text(f"Captured {len(rules)} rule(s)")
        return False

    def _build_captured_window_rules(self: Any, window_info: dict) -> list[WindowRule]:
        rules: list[WindowRule] = []

        window_class = str(window_info.get("class", "") or "").strip()
        if window_class:
            rules.append(WindowRule(field="class", pattern=re.escape(window_class)))

        window_title = str(window_info.get("title", "") or "").strip()
        if window_title:
            rules.append(WindowRule(field="title", pattern=re.escape(window_title)))

        if "window_tags" in self._compositor_capabilities:
            tags = [
                str(tag).strip().replace("*", "")
                for tag in list(window_info.get("tags", []) or [])
                if str(tag or "").strip().replace("*", "")
            ]
            if tags:
                rules.append(WindowRule(field="tag", pattern=re.escape(tags[0])))

        return rules

    def _set_window_rule_rows(self: Any, rules: list[WindowRule]) -> None:
        if not hasattr(self, "_rules_list_box"):
            return

        for row in list(getattr(self, "_rule_rows", [])):
            self._remove_rule_row_widget(row)

        self._rule_rows = []
        if not rules:
            empty_label = self._create_empty_row()
            self._rules_list_box.append(empty_label)
            self._rule_rows.append(empty_label)
            self._update_window_rules_remove_button()
            return

        for rule in rules:
            row_widget = self._create_rule_row(rule)
            self._rules_list_box.append(row_widget)
            self._rule_rows.append(row_widget)

        self._update_first_rule_delete_button()
        self._update_window_rules_remove_button()

    def _remove_rule_row_widget(self: Any, row: Gtk.Widget) -> None:
        if not hasattr(self, "_rules_list_box"):
            return
        list_box_row = row.get_parent()
        if isinstance(list_box_row, Gtk.ListBoxRow):
            self._rules_list_box.remove(list_box_row)
        else:
            self._rules_list_box.remove(row)

    def _create_rule_row(self: Any, rule: WindowRule) -> Gtk.Box:
        is_tag = rule.field == "tag"
        type_indicator = "🏷️" if is_tag else "🌐"

        row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row_box.add_css_class("card")
        row_box.set_margin_top(4)
        row_box.set_margin_bottom(4)
        row_box.set_margin_start(4)
        row_box.set_margin_end(4)

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_box.set_margin_top(8)
        header_box.set_margin_bottom(4)
        header_box.set_margin_start(12)
        header_box.set_margin_end(12)

        title_label = Gtk.Label(label=f"🪟 {rule.field}: {type_indicator} {rule.pattern}")
        title_label.set_hexpand(True)
        title_label.set_halign(Gtk.Align.START)
        header_box.append(title_label)

        delete_btn = Gtk.Button(icon_name="user-trash-symbolic")
        delete_btn.add_css_class("destructive-action")
        delete_btn.add_css_class("flat")
        delete_btn.connect("clicked", self._on_delete_rule, row_box)
        header_box.append(delete_btn)

        row_box.append(header_box)

        content_grid = Gtk.Grid()
        content_grid.set_column_spacing(12)
        content_grid.set_row_spacing(8)
        content_grid.set_margin_start(12)
        content_grid.set_margin_end(12)
        content_grid.set_margin_bottom(12)

        field_label = Gtk.Label(label="Field:")
        field_label.set_halign(Gtk.Align.START)
        content_grid.attach(field_label, 0, 0, 1, 1)

        field_dropdown = Gtk.DropDown()
        field_model = Gtk.StringList()
        field_model.append("class")
        field_model.append("title")

        has_tag_support = "window_tags" in self._compositor_capabilities
        if has_tag_support:
            field_model.append("tag")

        field_dropdown.set_model(field_model)
        field_dropdown.set_hexpand(True)
        if rule.field == "class":
            field_dropdown.set_selected(0)
        elif rule.field == "title":
            field_dropdown.set_selected(1)
        elif rule.field == "tag" and has_tag_support:
            field_dropdown.set_selected(2)
        else:
            field_dropdown.set_selected(0)
        content_grid.attach(field_dropdown, 1, 0, 1, 1)

        pattern_label = Gtk.Label(label="Pattern:")
        pattern_label.set_halign(Gtk.Align.START)
        content_grid.attach(pattern_label, 0, 1, 1, 1)

        pattern_entry = Gtk.Entry()
        pattern_entry.set_text(rule.pattern)
        if is_tag:
            pattern_entry.set_placeholder_text("e.g., game|browser|work")
        else:
            pattern_entry.set_placeholder_text("e.g., .*cs2.*")
        pattern_entry.set_hexpand(True)
        content_grid.attach(pattern_entry, 1, 1, 1, 1)
        row_box.append(content_grid)

        row_box._field_dropdown = field_dropdown
        row_box._pattern_entry = pattern_entry
        row_box._delete_btn = delete_btn
        row_box._title_label = title_label
        row_box._is_rule_row = True

        def on_field_changed(dropdown, _param) -> None:
            is_tag_field = has_tag_support and dropdown.get_selected() == 2
            if is_tag_field:
                pattern_entry.set_placeholder_text("e.g., game|browser|work")
            else:
                pattern_entry.set_placeholder_text("e.g., .*cs2.*")
            self._update_rule_row_title(row_box)

        field_dropdown.connect("notify::selected", on_field_changed)
        pattern_entry.connect("changed", self._on_rule_pattern_changed, row_box)
        self._update_rule_row_title(row_box)
        return row_box

    def _on_close_dialog_clicked(self: Any, _button: Gtk.Button, dialog: Adw.Dialog) -> None:
        dialog.close()

    def _on_close_current_rules_dialog_clicked(self: Any, _button: Gtk.Button) -> None:
        self._current_rules_dialog.close()

    def _on_rule_pattern_changed(self: Any, _entry: Gtk.Entry, row_box: Gtk.Box) -> None:
        self._update_rule_row_title(row_box)

    def _update_rule_row_title(self: Any, row: Gtk.Box) -> None:
        if not hasattr(row, "_title_label") or not hasattr(row, "_field_dropdown"):
            return

        field_idx = row._field_dropdown.get_selected()
        if field_idx == 0:
            field = "class"
        elif field_idx == 1:
            field = "title"
        elif "window_tags" in self._compositor_capabilities and field_idx == 2:
            field = "tag"
        else:
            field = "class"

        pattern = row._pattern_entry.get_text().strip() if hasattr(row, "_pattern_entry") else ""
        type_indicator = "🏷️" if field == "tag" else "🌐"
        row._title_label.set_label(f"🪟 {field}: {type_indicator} {pattern or '...'}")

    def _create_empty_row(self: Any) -> Gtk.Label:
        label = Gtk.Label(label="No window rules configured")
        label.add_css_class("dim-label")
        label.set_margin_top(12)
        label.set_margin_bottom(12)
        return label

    def _on_add_window_rule(self: Any, _button: Gtk.Button) -> None:
        if not hasattr(self, "_rules_list_box"):
            return

        for row in list(self._rule_rows):
            if isinstance(row, Gtk.Label) and "No window rules" in (row.get_text() or ""):
                self._rules_list_box.remove(row)
                self._rule_rows.remove(row)
                break

        new_row = self._create_rule_row(WindowRule(field="class", pattern=".*"))
        self._rules_list_box.append(new_row)
        self._rule_rows.append(new_row)
        self._update_first_rule_delete_button()
        self._update_window_rules_remove_button()

    def _on_remove_window_rules_clicked(self: Any, _button: Gtk.Button) -> None:
        if not hasattr(self, "_rules_list_box"):
            return

        for row in list(self._rule_rows):
            self._remove_rule_row_widget(row)
        self._rule_rows = []
        empty_label = self._create_empty_row()
        self._rules_list_box.append(empty_label)
        self._rule_rows.append(empty_label)
        self._update_window_rules_remove_button()
        self._on_apply_window_rules(_button)

    def _on_delete_rule(self: Any, _button: Gtk.Button, row: Gtk.Box) -> None:
        if not hasattr(self, "_rules_list_box"):
            return
        if row in self._rule_rows:
            self._rule_rows.remove(row)
        self._remove_rule_row_widget(row)

        rule_rows = [item for item in self._rule_rows if hasattr(item, "_is_rule_row")]
        if not rule_rows:
            empty_label = self._create_empty_row()
            self._rules_list_box.append(empty_label)
            self._rule_rows.append(empty_label)
        else:
            self._update_first_rule_delete_button()
        self._update_window_rules_remove_button()

    def _update_first_rule_delete_button(self: Any) -> None:
        rule_rows = [row for row in self._rule_rows if hasattr(row, "_is_rule_row")]
        show_delete = len(rule_rows) > 1
        for row in rule_rows:
            if hasattr(row, "_delete_btn"):
                row._delete_btn.set_visible(show_delete)

    def _update_window_rules_remove_button(self: Any) -> None:
        if not hasattr(self, "_remove_window_rules_btn") or self._remove_window_rules_btn is None:
            return
        has_rules = any(hasattr(row, "_is_rule_row") for row in getattr(self, "_rule_rows", []))
        self._remove_window_rules_btn.set_sensitive(has_rules)

    def _on_apply_window_rules(self: Any, _button: Gtk.Button) -> None:
        target_profile = self._window_rules_target_profile()
        if not target_profile or self.profile_manager is None:
            if hasattr(self, "_current_rules_dialog"):
                self._current_rules_dialog.close()
            return

        new_rules = []
        has_tag_support = "window_tags" in self._compositor_capabilities
        for row in self._rule_rows:
            if not hasattr(row, "_is_rule_row") or not hasattr(row, "_field_dropdown"):
                continue
            field_idx = row._field_dropdown.get_selected()
            if field_idx == 0:
                field = "class"
            elif field_idx == 1:
                field = "title"
            elif has_tag_support and field_idx == 2:
                field = "tag"
            else:
                field = "class"

            pattern = row._pattern_entry.get_text().strip()
            if pattern:
                new_rules.append(WindowRule(field=field, pattern=pattern))

        try:
            self.profile_manager.validate_window_rules(new_rules)
        except ValueError as exc:
            self._show_profile_error_dialog(str(exc))
            return

        target_profile.config.window_rules = new_rules
        target_profile.config.is_permanent = not new_rules
        if not self._save_specific_profile(target_profile):
            return
        if (
            self._selected_profile
            and self._selected_profile.config.name == target_profile.config.name
        ):
            self._update_rules_label()
            self._update_profile_state_display()

        if hasattr(self, "_current_rules_dialog"):
            self._cancel_window_rule_capture("")
            self._current_rules_dialog.close()

    def _cancel_window_rule_capture(self: Any, status_text: str) -> None:
        if self._window_rule_capture_timeout_id:
            GLib.source_remove(self._window_rule_capture_timeout_id)
            self._window_rule_capture_timeout_id = 0
        self._window_rule_capture_pending = False
        if hasattr(self, "_window_rule_capture_btn"):
            self._window_rule_capture_btn.set_sensitive(True)
        if hasattr(self, "_window_rule_capture_status"):
            self._window_rule_capture_status.set_text(status_text)

    def _update_rules_label(self: Any) -> None:
        if not self._selected_profile:
            self.rules_list_label.set_text("No profile selected")
            return

        rules = self._selected_profile.config.window_rules
        if not rules:
            self.rules_list_label.set_text("No rules - always active")
            return

        parts = [f"{rule.field}={rule.pattern}" for rule in rules[:2]]
        if len(rules) > 2:
            parts.append(f"... (+{len(rules) - 2})")
        self.rules_list_label.set_text(f"{', '.join(parts)} - conditional")
