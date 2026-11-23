from __future__ import annotations

import copy
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.analog import AnalogControlConfig
from keymasq.common.model.hardware import HardwareConfig
from keymasq.common.model.profiles import DeviceProfileLayer
from keymasq.gui.session_client import GuiTaskResult, run_gui_task
from keymasq.gui.widgets.action_labels import describe_mapping_action_compact
from keymasq.session.analog_controls import AnalogControlManager
from keymasq.session.presets import (
    PresetPlan,
    ProfilePreset,
    load_presets,
    plan_preset,
    presets_for_hardware,
    save_preset_analog_controls,
    write_preset_mappings,
)
from keymasq.session.profile.manager import ProfileManager

type _LoadedPresetData = tuple[list[ProfilePreset], dict[str, AnalogControlConfig]]


def _escape(text: str) -> str:
    return GLib.markup_escape_text(text)


def _action_row(title: str, subtitle: str = "") -> Adw.ActionRow:
    row = Adw.ActionRow(use_markup=False, subtitle_lines=0)
    row.set_title(title)
    if subtitle:
        row.set_subtitle(subtitle)
    return row


def _count_text(count: int, singular: str, plural: str | None = None) -> str:
    noun = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {noun}"


def _hardware_control_labels(hardware: HardwareConfig) -> dict[str, str]:
    labels: dict[str, str] = {}
    for button in hardware.buttons:
        labels[button.id] = button.label or button.id
    for analog in hardware.analog_inputs:
        labels[analog.id] = analog.label or analog.id
    return labels


def preset_control_label(hardware: HardwareConfig, source_id: str) -> str:
    return _hardware_control_labels(hardware).get(source_id, source_id)


def empty_plan_summary(plan: PresetPlan) -> str:
    if plan.kept and plan.unavailable:
        return (
            "No controls will be filled. Every control in this preset is either "
            "already mapped in this profile or unavailable on this device."
        )
    if plan.kept:
        return (
            "No controls will be filled. Every matching control in this preset is already mapped."
        )
    if plan.unavailable:
        return (
            "No controls will be filled. "
            "None of the controls in this preset are available on this device."
        )
    return "No controls will be filled."


def preset_applied_feedback(preset_name: str, plan: PresetPlan) -> str:
    filled = len(plan.mappings)
    if filled == 0:
        return f"No mappings were applied from '{preset_name}'"
    kept = len(plan.kept)
    if kept > 0:
        return f"Applied '{preset_name}': {filled} filled, {kept} kept"
    return f"Applied '{preset_name}': {_count_text(filled, 'mapping')} filled"


class ApplyPresetDialog(Adw.Dialog):
    def __init__(
        self,
        hardware: HardwareConfig,
        profile_name: str,
        layer: DeviceProfileLayer | None,
        *,
        profile_manager: ProfileManager,
        on_applied: Callable[[ProfilePreset, PresetPlan], None],
    ) -> None:
        super().__init__(title="Apply Preset", content_width=520, content_height=540)
        self._hardware = hardware
        self._profile_name = profile_name
        self._layer = copy.deepcopy(layer) if layer is not None else None
        self._profile_manager = profile_manager
        self._on_applied = on_applied
        self._closed = False
        self._applying = False
        self._presets: list[ProfilePreset] = []
        self._saved_analog_controls: dict[str, AnalogControlConfig] = {}
        self._preset_rows: list[Adw.ActionRow] = []

        self.connect("closed", self._on_dialog_closed)

        self._nav_view = Adw.NavigationView()
        self._nav_view.add(self._build_list_page())
        self.set_child(self._nav_view)
        self._load_presets_async()

    def _on_dialog_closed(self, _dialog: Adw.Dialog) -> None:
        self._closed = True
        self._spinner.stop()

    def _build_list_page(self) -> Adw.NavigationPage:
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        self._list_stack = Gtk.Stack()
        self._list_stack.set_vexpand(True)

        loading_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        self._spinner = Gtk.Spinner()
        self._spinner.set_size_request(32, 32)
        self._spinner.start()
        loading_box.append(self._spinner)
        loading_label = Gtk.Label(label="Loading presets…")
        loading_label.add_css_class("dim-label")
        loading_box.append(loading_label)
        self._list_stack.add_named(loading_box, "loading")

        self._empty_page = Adw.StatusPage(
            icon_name="view-list-symbolic",
            title="No Matching Presets",
            description=_escape(
                f"No presets match {self._hardware.name}. "
                "Add .toml preset files to ~/.config/keymasq/presets to create your own."
            ),
        )
        self._list_stack.add_named(self._empty_page, "empty")

        self._error_page = Adw.StatusPage(
            icon_name="dialog-warning-symbolic",
            title="Could Not Load Presets",
        )
        self._list_stack.add_named(self._error_page, "error")

        list_page = Adw.PreferencesPage()
        self._presets_group = Adw.PreferencesGroup(
            title="Presets",
            description=_escape(
                f"Choose a preset for {self._hardware.name} in {self._profile_name}. "
                "Existing mappings are kept."
            ),
        )
        list_page.add(self._presets_group)
        self._list_stack.add_named(list_page, "list")

        self._list_stack.set_visible_child_name("loading")
        toolbar.set_content(self._list_stack)
        return Adw.NavigationPage(child=toolbar, title="Apply Preset", tag="list")

    def _load_presets_async(self) -> None:
        hardware = self._hardware

        def worker() -> _LoadedPresetData:
            matching = presets_for_hardware(load_presets(), hardware)
            saved_analog = AnalogControlManager().get_all_analog_controls()
            return matching, saved_analog

        run_gui_task(worker, self._on_presets_loaded)

    def _on_presets_loaded(self, result: GuiTaskResult[_LoadedPresetData]) -> bool:
        self._spinner.stop()
        if self._closed:
            return False
        if result.error is not None or result.value is None:
            self._error_page.set_description(
                _escape(str(result.error) if result.error is not None else "Unknown error")
            )
            self._list_stack.set_visible_child_name("error")
            return False

        presets, saved_analog = result.value
        self._presets = presets
        self._saved_analog_controls = saved_analog

        for row in self._preset_rows:
            self._presets_group.remove(row)
        self._preset_rows = []

        if not presets:
            self._list_stack.set_visible_child_name("empty")
            return False

        for preset in presets:
            row = _action_row(preset.name, preset.description)
            row.set_activatable(True)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", self._on_preset_row_activated, preset)
            self._presets_group.add(row)
            self._preset_rows.append(row)

        self._list_stack.set_visible_child_name("list")
        return False

    def _on_preset_row_activated(self, _row: Adw.ActionRow, preset: ProfilePreset) -> None:
        plan = plan_preset(
            preset,
            self._hardware,
            self._layer,
            self._saved_analog_controls,
        )
        preview_page = self._build_preview_page(preset, plan)
        self._nav_view.push(preview_page)

    def _build_preview_page(self, preset: ProfilePreset, plan: PresetPlan) -> Adw.NavigationPage:
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        page = Adw.PreferencesPage()
        control_labels = _hardware_control_labels(self._hardware)

        summary_lines = [f"Profile: {self._profile_name} · Device: {self._hardware.name}"]
        if preset.description:
            summary_lines.insert(0, preset.description)
        summary_group = Adw.PreferencesGroup(
            title="Summary",
            description=_escape("\n".join(summary_lines)),
        )
        if plan.mappings:
            summary_parts = [f"{_count_text(len(plan.mappings), 'control')} will be filled"]
            if plan.kept:
                summary_parts.append(f"{len(plan.kept)} kept")
            if plan.unavailable:
                summary_parts.append(f"{len(plan.unavailable)} unavailable")
            overview_row = _action_row(
                ", ".join(summary_parts),
                (
                    "Creates analog controls: "
                    + ", ".join(control.name for control in plan.new_analog_controls)
                    if plan.new_analog_controls
                    else "Only unbound and passthrough controls are filled."
                ),
            )
            summary_group.add(overview_row)
        else:
            summary_group.add(_action_row("Nothing to fill", empty_plan_summary(plan)))
        page.add(summary_group)

        if plan.mappings:
            fill_group = Adw.PreferencesGroup(
                title=f"Will fill ({len(plan.mappings)})",
                description="These controls will receive mappings from the preset.",
            )
            for source_id, action in plan.mappings.items():
                fill_group.add(
                    _action_row(
                        control_labels.get(source_id, source_id),
                        describe_mapping_action_compact(action, include_state=True),
                    )
                )
            page.add(fill_group)

        if plan.kept:
            kept_group = Adw.PreferencesGroup(
                title=f"Kept ({len(plan.kept)})",
                description="Already mapped in this profile and will not be overwritten.",
            )
            existing_mappings = self._layer.mappings if self._layer is not None else {}
            for source_id in plan.kept:
                existing = existing_mappings.get(source_id)
                subtitle = (
                    f"Current: {describe_mapping_action_compact(existing, include_state=True)}"
                    if existing is not None
                    else "Already mapped"
                )
                kept_group.add(_action_row(control_labels.get(source_id, source_id), subtitle))
            page.add(kept_group)

        if plan.unavailable:
            unavailable_group = Adw.PreferencesGroup(
                title=f"Unavailable ({len(plan.unavailable)})",
                description="This device lacks these controls or cannot use the preset action.",
            )
            for source_id in plan.unavailable:
                subtitle = (
                    "Incompatible action for this control"
                    if source_id in control_labels
                    else "Not present on this device"
                )
                unavailable_group.add(
                    _action_row(control_labels.get(source_id, source_id), subtitle)
                )
            page.add(unavailable_group)

        toolbar.set_content(page)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_margin_top(8)
        footer.set_margin_bottom(8)
        footer.set_margin_start(12)
        footer.set_margin_end(12)

        error_label = Gtk.Label(xalign=0.0, hexpand=True)
        error_label.add_css_class("error")
        error_label.add_css_class("caption")
        error_label.set_wrap(True)
        error_label.set_visible(False)
        footer.append(error_label)

        close_btn = Gtk.Button(label="Cancel" if plan.mappings else "Close")
        close_btn.set_valign(Gtk.Align.CENTER)
        close_btn.connect("clicked", self._on_cancel_clicked)
        footer.append(close_btn)

        if plan.mappings:
            apply_btn = Gtk.Button(label="Apply Preset")
            apply_btn.add_css_class("suggested-action")
            apply_btn.set_valign(Gtk.Align.CENTER)
            apply_btn.connect(
                "clicked",
                self._on_apply_clicked,
                preset,
                apply_btn,
                close_btn,
                error_label,
            )
            footer.append(apply_btn)

        toolbar.add_bottom_bar(footer)
        return Adw.NavigationPage(child=toolbar, title=preset.name, tag="preview")

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        if not self._applying:
            self.close()

    def _on_apply_clicked(
        self,
        _button: Gtk.Button,
        preset: ProfilePreset,
        apply_btn: Gtk.Button,
        cancel_btn: Gtk.Button,
        error_label: Gtk.Label,
    ) -> None:
        if self._applying:
            return
        self._applying = True
        apply_btn.set_sensitive(False)
        cancel_btn.set_sensitive(False)
        self.set_can_close(False)
        error_label.set_visible(False)

        hardware = self._hardware
        layer = self._layer

        def worker() -> dict[str, AnalogControlConfig]:
            return save_preset_analog_controls(preset, hardware, layer, AnalogControlManager())

        def show_error(message: str) -> None:
            if self._closed:
                return
            apply_btn.set_sensitive(True)
            cancel_btn.set_sensitive(True)
            error_label.set_text(f"Could not apply preset: {message}")
            error_label.set_visible(True)

        def on_done(result: GuiTaskResult[dict[str, AnalogControlConfig]]) -> bool:
            self._applying = False
            self.set_can_close(True)
            if result.error is not None or result.value is None:
                show_error(str(result.error) if result.error is not None else "Unknown error")
                return False

            # The profile is shared with the tab, so it is only changed on the main thread.
            try:
                applied_plan = self._write_mappings(preset, result.value)
            except (OSError, ValueError) as exc:
                show_error(str(exc))
                return False
            if not self._closed:
                self.close()
            self._on_applied(preset, applied_plan)
            return False

        run_gui_task(worker, on_done)

    def _write_mappings(
        self,
        preset: ProfilePreset,
        saved_analog_controls: dict[str, AnalogControlConfig],
    ) -> PresetPlan:
        current = self._profile_manager.get_profile(self._profile_name)
        if current is None:
            raise ValueError(f"Profile '{self._profile_name}' no longer exists.")
        return write_preset_mappings(
            preset,
            current.config,
            self._hardware,
            saved_analog_controls,
            profile_manager=self._profile_manager,
        )
