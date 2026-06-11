# pyright: reportAttributeAccessIssue=false
from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.models import ActionType, AnalogControlConfig, MappingAction
from keymasq.session.analog_controls import (
    AnalogControlManager,
    AnalogControlPreset,
    analog_control_presets,
)

from .compat import notify_session_reload_async

log = logging.getLogger("keymasq.gui.widgets.key_selector_dialog")


class AnalogTabMixin:
    def _set_initial_analog_tab(self) -> None:
        action = self._current_action
        if action is not None and action.action_type == ActionType.ANALOG_CONTROL:
            self.stack.set_visible_child_name("analog_control")
            return
        if action is not None and action.action_type in (
            ActionType.SUPPRESS,
            ActionType.PASSTHROUGH,
        ):
            self.stack.set_visible_child_name("special")
            return
        if self._analog_control_list:
            self.stack.set_visible_child_name("analog_control")
        else:
            self.stack.set_visible_child_name("analog_presets")

    def _build_analog_presets_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        intro = Gtk.Label(
            label="Pick what this control does — one click sets it up. "
            "Fine-tune it any time under Analog Controls."
        )
        intro.add_css_class("dim-label")
        intro.set_wrap(True)
        intro.set_justify(Gtk.Justification.CENTER)
        intro.set_halign(Gtk.Align.CENTER)
        intro.set_margin_top(16)
        intro.set_margin_start(16)
        intro.set_margin_end(16)
        intro.set_margin_bottom(12)
        outer.append(intro)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_max_children_per_line(2)
        flow.set_min_children_per_line(2)
        flow.set_homogeneous(True)
        flow.set_column_spacing(12)
        flow.set_row_spacing(12)
        flow.set_margin_start(16)
        flow.set_margin_end(16)
        flow.set_margin_bottom(16)
        flow.set_valign(Gtk.Align.START)

        for preset in analog_control_presets(self._analog_input_type):
            flow.append(self._build_analog_preset_card(preset))

        scrolled.set_child(flow)
        outer.append(scrolled)

        link_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        link_row.set_halign(Gtk.Align.CENTER)
        link_row.set_margin_top(4)
        link_row.set_margin_bottom(12)
        manage_btn = Gtk.Button(label="Open Analog Controls…")
        manage_btn.add_css_class("flat")
        manage_btn.set_tooltip_text("Create or fine-tune analog controls")
        manage_btn.connect("clicked", self._on_open_analog_manager_clicked)
        link_row.append(manage_btn)
        outer.append(link_row)

        return outer

    def _build_analog_preset_card(self, preset: AnalogControlPreset) -> Gtk.Widget:
        button = Gtk.Button()
        button.add_css_class("card")
        button.set_hexpand(True)
        button.set_tooltip_text(preset.description)
        button.connect("clicked", self._on_analog_preset_clicked, preset)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        icon = Gtk.Image.new_from_icon_name(preset.icon_name)
        icon.set_pixel_size(28)
        content.append(icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label=preset.label)
        title.set_halign(Gtk.Align.START)
        title.add_css_class("heading")
        text_box.append(title)
        subtitle = Gtk.Label(label=preset.description)
        subtitle.set_halign(Gtk.Align.START)
        subtitle.set_wrap(True)
        subtitle.add_css_class("dim-label")
        subtitle.add_css_class("caption")
        text_box.append(subtitle)
        content.append(text_box)

        button.set_child(content)
        return button

    def _on_analog_preset_clicked(self, _button: Gtk.Button, preset: AnalogControlPreset) -> None:
        manager = AnalogControlManager()
        name = manager.unique_analog_control_name(preset.default_name)
        config = preset.build(name)
        try:
            manager.save_analog_control(config)
        except (OSError, ValueError) as exc:
            log.exception("Could not save preset analog control %s", name)
            self._show_analog_preset_save_error(name, exc)
            return
        notify_session_reload_async()
        action = MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_names=[name],
        )
        self.emit("key-selected", action)
        self.close()

    def _show_analog_preset_save_error(self, name: str, exc: Exception) -> None:
        dialog = Adw.AlertDialog()
        dialog.set_heading("Unable To Save Analog Control")
        dialog.set_body(f"Could not save preset '{name}': {exc}")
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

    def _on_open_analog_manager_clicked(self, _button: Gtk.Button) -> None:
        self._open_analog_control_manager()

    def _open_analog_control_manager(self, select_name: str | None = None) -> None:
        from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

        root = self.get_root()
        profile_manager = self._profile_manager_for_child_dialog()
        dialog = AnalogControlDialog(root, profile_manager)
        dialog.connect("analog-control-saved", self._on_analog_control_manager_changed)
        dialog.connect("analog-control-deleted", self._on_analog_control_manager_changed)
        dialog.present(root)
        if select_name:
            dialog.select_control_by_name(select_name)

    def _on_analog_control_manager_changed(self, _dialog, _name: str) -> None:
        notify_session_reload_async()
        self._load_analog_control_list()
        if self.stack.get_visible_child_name() == "analog_presets":
            self.stack.set_visible_child_name("analog_control")
        self._on_tab_changed(self.stack, None)

    def _build_analog_control_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        toolbar_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar_row.set_margin_top(8)
        toolbar_row.set_margin_bottom(4)
        toolbar_row.set_margin_start(12)
        toolbar_row.set_margin_end(12)
        toolbar_row.set_halign(Gtk.Align.START)

        manage_btn = Gtk.Button(label="Open Analog Controls…")
        manage_btn.add_css_class("flat")
        manage_btn.set_tooltip_text("Create or fine-tune analog controls")
        manage_btn.connect("clicked", self._on_open_analog_manager_clicked)
        toolbar_row.append(manage_btn)

        selection_hint = Gtk.Label(
            label="Select one or multiple · right-click to edit"
        )
        selection_hint.add_css_class("dim-label")
        selection_hint.add_css_class("caption")
        selection_hint.set_halign(Gtk.Align.START)
        toolbar_row.append(selection_hint)
        outer.append(toolbar_row)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        self._analog_control_listbox = Gtk.ListBox()
        self._analog_control_listbox.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self._analog_control_listbox.set_valign(Gtk.Align.START)
        self._analog_control_listbox.add_css_class("boxed-list")
        self._analog_control_listbox.set_margin_start(12)
        self._analog_control_listbox.set_margin_end(12)
        self._analog_control_listbox.connect(
            "row-selected",
            self._on_analog_control_row_selected,
        )
        scrolled.set_child(self._analog_control_listbox)
        outer.append(scrolled)

        self._load_analog_control_list()
        return outer

    def _load_analog_control_list(self) -> None:
        manager = AnalogControlManager()
        configs = manager.get_all_analog_controls()
        self._analog_control_names = manager.list_analog_controls()
        self._analog_control_list = [
            config
            for name in self._analog_control_names
            if (config := configs.get(name)) is not None
            and (
                self._analog_input_type is None
                or config.input_type == self._analog_input_type
            )
        ]
        self._populate_analog_control_listbox()

    def _populate_analog_control_listbox(self) -> None:
        selected_names = set(self._selected_analog_controls)
        while self._analog_control_listbox.get_first_child():
            self._analog_control_listbox.remove(self._analog_control_listbox.get_first_child())

        if not self._analog_control_list:
            self._selected_analog_control = None
            self._selected_analog_controls = []
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            label = "No analog controls saved yet"
            if self._analog_input_type == "axis":
                label = "No axis controls saved yet"
            elif self._analog_input_type == "stick":
                label = "No stick controls saved yet"
            lbl = Gtk.Label(label=label)
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(12)
            lbl.set_margin_bottom(12)
            row.set_child(lbl)
            self._analog_control_listbox.append(row)
            return

        for config in self._analog_control_list:
            row = Gtk.ListBoxRow()
            row._analog_control_name = config.name
            click = Gtk.GestureClick()
            click.set_button(1)
            click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            click.connect("pressed", self._on_analog_control_row_pressed, row)
            row.add_controller(click)

            right_click = Gtk.GestureClick()
            right_click.set_button(3)
            right_click.connect(
                "pressed", self._on_analog_control_row_right_pressed, config.name
            )
            row.add_controller(right_click)

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row_box.set_margin_top(8)
            row_box.set_margin_bottom(8)
            row_box.set_margin_start(12)
            row_box.set_margin_end(12)

            name_label = Gtk.Label(label=config.name)
            name_label.set_halign(Gtk.Align.START)
            name_label.set_hexpand(True)
            row_box.append(name_label)

            info_label = Gtk.Label(label=self._describe_analog_control_row(config))
            info_label.add_css_class("dim-label")
            info_label.add_css_class("caption")
            row_box.append(info_label)

            row.set_child(row_box)
            self._analog_control_listbox.append(row)

            if config.name in selected_names:
                self._analog_control_listbox.select_row(row)
        self._sync_selected_analog_controls()

    def _describe_analog_control_row(self, config: AnalogControlConfig) -> str:
        parts: list[str] = ["Axis" if config.input_type == "axis" else "Stick"]
        if config.mouse_motion.enabled:
            parts.append("Mouse")
        if config.gamepad_output.enabled:
            parts.append("Gamepad output")
        if config.thresholds:
            count = len(config.thresholds)
            parts.append(f"{count} range{'s' if count != 1 else ''}")
        return " · ".join(parts)

    def _on_analog_control_row_right_pressed(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
        name: str,
    ) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._open_analog_control_manager(name)

    def _on_analog_control_row_pressed(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
        row: Gtk.ListBoxRow,
    ) -> None:
        if row.is_selected():
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._unselect_analog_control_row(row)

    def _unselect_analog_control_row(self, row: Gtk.ListBoxRow) -> bool:
        if row.is_selected():
            self._analog_control_listbox.unselect_row(row)
            self._sync_selected_analog_controls()
            if self.stack.get_visible_child_name() == "analog_control":
                self.map_btn.set_sensitive(bool(self._selected_analog_controls))
        return False

    def _on_analog_control_row_selected(self, listbox, row) -> None:
        self._sync_selected_analog_controls()
        if self.stack.get_visible_child_name() == "analog_control":
            self.map_btn.set_sensitive(bool(self._selected_analog_controls))

    def _sync_selected_analog_controls(self) -> None:
        selected: list[str] = []
        for row in self._analog_control_listbox.get_selected_rows():
            name = getattr(row, "_analog_control_name", None)
            if isinstance(name, str):
                selected.append(name)
        self._selected_analog_controls = selected
        self._selected_analog_control = selected[0] if selected else None

    def _on_analog_control_map_clicked(self, btn) -> None:
        if not self._selected_analog_controls:
            return
        action = MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_names=list(self._selected_analog_controls),
        )
        self.emit("key-selected", action)
        self.close()
