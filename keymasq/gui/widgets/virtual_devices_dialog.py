from __future__ import annotations

from collections.abc import Callable, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.virtual_device_templates import (
    BUILTIN_VIRTUAL_DEVICE_TEMPLATES,
    LOGITECH_EXTREME_3D_TEMPLATE_ID,
    MAX_TEMPLATE_AXES,
    MAX_TEMPLATE_BUTTONS,
    MAX_USER_VIRTUAL_DEVICES,
    XBOX_360_TEMPLATE_ID,
    VirtualAxis,
    VirtualButton,
    VirtualDeviceConfig,
    VirtualDeviceConfigError,
    VirtualDeviceInstance,
    VirtualDeviceTemplate,
    config_from_json,
    config_to_json,
    instance_from_data,
    instance_to_data,
    numbered_button_batch,
    template_from_data,
    template_to_data,
)
from keymasq.gui.session_client import session_request_async
from keymasq.gui.widgets.virtual_template_controls import TemplateControlRow, event_names
from keymasq.session.virtual_devices import load_virtual_device_config


def unique_template_copy(
    template: VirtualDeviceTemplate,
    templates: Sequence[VirtualDeviceTemplate],
) -> VirtualDeviceTemplate:
    used = {item.id for item in templates}
    base = f"{template.id[:54]}-copy"
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}-{index}"
        index += 1
    data = template_to_data(template)
    data["id"] = candidate
    label = template.label.encode("utf-8")[:75].decode("utf-8", errors="ignore")
    data["label"] = f"{label} copy"
    return template_from_data(data)


def _entry_row(title: str, value: str = "") -> tuple[Adw.EntryRow, Gtk.Entry]:
    row = Adw.EntryRow(title=title)
    row.set_text(value)
    return row, row


class VirtualTemplateEditorDialog(Adw.Dialog):
    def __init__(
        self,
        template: VirtualDeviceTemplate | None,
        on_save: Callable[[VirtualDeviceTemplate], bool],
        *,
        creating: bool = False,
    ) -> None:
        super().__init__(
            title="New template" if creating or template is None else "Edit template",
            content_width=760,
            content_height=680,
        )
        self._on_save = on_save

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        pages = Gtk.Stack(vexpand=True)
        page = Adw.PreferencesPage()
        buttons_page = Adw.PreferencesPage()
        axes_page = Adw.PreferencesPage()
        pages.add_titled(page, "identity", "Identity")
        pages.add_titled(buttons_page, "buttons", "Buttons")
        pages.add_titled(axes_page, "axes", "Axes")
        switcher = Gtk.StackSwitcher(stack=pages, halign=Gtk.Align.CENTER)
        switcher.set_margin_top(8)
        switcher.set_margin_bottom(8)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(switcher)
        content.append(pages)

        identity = Adw.PreferencesGroup(
            title="Template", description="Define the controls and identity a game will see."
        )
        page.add(identity)
        values = template_to_data(template) if template else {}
        self._id_row, _ = _entry_row("Template ID", str(values.get("id", "custom-controller")))
        self._id_row.set_editable(creating or template is None)
        self._label_row, _ = _entry_row(
            "Display name", str(values.get("label", "Custom controller"))
        )
        self._name_row, _ = _entry_row(
            "Linux device name", str(values.get("name", "Custom controller"))
        )
        self._vendor_row, _ = _entry_row("Vendor ID (hex)", str(values.get("vendor_id", "0000")))
        self._product_row, _ = _entry_row("Product ID (hex)", str(values.get("product_id", "0000")))
        self._version_row, _ = _entry_row("Version", str(values.get("version", "0100")))
        self._bustype_row, _ = _entry_row("Bus type", str(values.get("bustype", "usb")))
        identity.add(self._label_row)
        self._layout_row = Adw.ComboRow(
            title="Mapping layout",
            subtitle="Changing the layout keeps your configured buttons and axes",
        )
        self._layout_row.set_model(Gtk.StringList.new(["Gamepad", "Flight stick"]))
        self._layout_row.set_selected(1 if template and template.layout == "flight-stick" else 0)
        identity.add(self._layout_row)
        advanced = Adw.ExpanderRow(
            title="Device identity", subtitle="Linux device name, template ID, USB IDs and bus type"
        )
        self._id_row.set_tooltip_text("Stable ID; cannot be changed after creating a template")
        for row in (
            self._name_row,
            self._id_row,
            self._vendor_row,
            self._product_row,
            self._version_row,
            self._bustype_row,
        ):
            advanced.add_row(row)
        identity.add(advanced)

        self._button_rows: list[TemplateControlRow] = []
        self._axis_rows: list[TemplateControlRow] = []
        self._buttons_group = Adw.PreferencesGroup(title="Buttons")
        self._axes_group = Adw.PreferencesGroup(
            title="Axes",
            description="X and Y are required. Expand an axis to edit its range and rest value.",
        )
        self._add_button = Gtk.Button(label="Add button", valign=Gtk.Align.CENTER)
        self._add_button.connect("clicked", self._new_button)
        button_actions = Gtk.Box(spacing=6)
        self._batch_button = Gtk.Button(label="Add numbered buttons", valign=Gtk.Align.CENTER)
        self._batch_button.connect("clicked", self._add_numbered_buttons)
        button_actions.append(self._batch_button)
        button_actions.append(self._add_button)
        self._buttons_group.set_header_suffix(button_actions)
        self._add_axis = Gtk.Button(label="Add axis", valign=Gtk.Align.CENTER)
        self._add_axis.connect("clicked", self._new_axis)
        self._axes_group.set_header_suffix(self._add_axis)
        buttons_page.add(self._buttons_group)
        axes_page.add(self._axes_group)
        buttons = (
            template.buttons if template else (VirtualButton("trigger", "Trigger", "btn_trigger"),)
        )
        axes = (
            template.axes
            if template
            else (
                VirtualAxis("x", "X", "abs_x", -32768, 32767),
                VirtualAxis("y", "Y", "abs_y", -32768, 32767),
            )
        )
        for control in (*buttons, *axes):
            self._append_control(control)

        footer = Gtk.ActionBar()
        self._status = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self._status.add_css_class("error")
        footer.pack_start(self._status)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", self._close_clicked)
        footer.pack_end(cancel)
        save = Gtk.Button(label="Use changes")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._save)
        footer.pack_end(save)

        toolbar.set_content(content)
        toolbar.add_bottom_bar(footer)
        self.set_child(toolbar)

    def _append_control(self, control: VirtualButton | VirtualAxis) -> None:
        row = TemplateControlRow(control, self._remove_control)
        if isinstance(control, VirtualAxis):
            self._axis_rows.append(row)
            self._axes_group.add(row)
        else:
            self._button_rows.append(row)
            self._buttons_group.add(row)
        self._update_control_limits()

    def _remove_control(self, row: TemplateControlRow) -> None:
        rows = self._axis_rows if row.is_axis else self._button_rows
        group = self._axes_group if row.is_axis else self._buttons_group
        rows.remove(row)
        group.remove(row)
        self._update_control_limits()

    def _update_control_limits(self) -> None:
        self._add_button.set_sensitive(len(self._button_rows) < MAX_TEMPLATE_BUTTONS)
        self._batch_button.set_sensitive(len(self._button_rows) < MAX_TEMPLATE_BUTTONS)
        self._add_axis.set_sensitive(len(self._axis_rows) < MAX_TEMPLATE_AXES)
        self._buttons_group.set_title(f"Buttons · {len(self._button_rows)}")
        self._axes_group.set_title(f"Axes · {len(self._axis_rows)}")

    def _new_button(self, _button: Gtk.Button) -> None:
        self._new_control(axis=False)

    def _add_numbered_buttons(self, _button: Gtk.Button) -> None:
        remaining = MAX_TEMPLATE_BUTTONS - len(self._button_rows)
        if remaining <= 0:
            return
        dialog = Adw.Dialog(title="Add numbered buttons", content_width=420)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            description=(
                f"Add up to {remaining} independent buttons. Each receives an unused "
                "TriggerHappy code. You can rename them afterward."
            )
        )
        count = Adw.SpinRow(
            title="Number of buttons",
            adjustment=Gtk.Adjustment(
                value=min(8, remaining), lower=1, upper=remaining, step_increment=1
            ),
            digits=0,
        )
        group.add(count)
        page.add(group)
        toolbar.set_content(page)
        footer = Gtk.ActionBar()
        add = Gtk.Button(label="Add buttons")
        add.add_css_class("suggested-action")

        def add_clicked(_button: Gtk.Button) -> None:
            count.update()
            self._append_numbered_buttons(int(count.get_value()))
            dialog.close()

        add.connect("clicked", add_clicked)
        footer.pack_end(add)
        toolbar.add_bottom_bar(footer)
        dialog.set_child(toolbar)
        dialog.present(self)

    def _append_numbered_buttons(self, count: int) -> None:
        buttons = [
            VirtualButton(
                row.id_row.get_text(),
                row.label_row.get_text(),
                row.codes[int(row.code_row.get_selected())],
            )
            for row in self._button_rows
        ]
        try:
            added = numbered_button_batch(
                buttons, count, reserved_ids={row.id_row.get_text() for row in self._axis_rows}
            )
        except VirtualDeviceConfigError as exc:
            self._status.set_text(str(exc))
            return
        for control in added:
            self._append_control(control)
        self._status.set_text("")

    def _new_axis(self, _button: Gtk.Button) -> None:
        self._new_control(axis=True)

    def _new_control(self, *, axis: bool) -> None:
        rows = self._axis_rows if axis else self._button_rows
        used_codes = {row.to_data()["evdev"] for row in rows}
        used_ids = {row.id_row.get_text() for row in (*self._button_rows, *self._axis_rows)}
        preferred = (
            ("abs_x", "abs_y", "abs_z", "abs_rx", "abs_ry", "abs_rz", "abs_hat0x", "abs_hat0y")
            if axis
            else ("btn_trigger", "btn_thumb", "btn_thumb2", "btn_top", "btn_top2", "btn_pinkie")
        )
        code = next(
            code for code in (*preferred, *event_names(axis=axis)) if code not in used_codes
        )
        control_id = code.replace("_", "-")
        index = 2
        while control_id in used_ids:
            control_id = f"{code.replace('_', '-')}-{index}"
            index += 1
        label = code.removeprefix("abs_").removeprefix("btn_").replace("_", " ").title()
        control = (
            VirtualAxis(control_id, label, code, -32768, 32767)
            if axis
            else VirtualButton(control_id, label, code)
        )
        self._append_control(control)
        rows[-1].set_expanded(True)
        rows[-1].label_row.grab_focus()

    def _close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _save(self, _button: Gtk.Button) -> None:
        try:
            template = template_from_data(
                {
                    "id": self._id_row.get_text(),
                    "layout": "flight-stick" if self._layout_row.get_selected() == 1 else "gamepad",
                    "label": self._label_row.get_text(),
                    "name": self._name_row.get_text(),
                    "vendor_id": self._vendor_row.get_text(),
                    "product_id": self._product_row.get_text(),
                    "version": self._version_row.get_text(),
                    "bustype": self._bustype_row.get_text(),
                    "buttons": [row.to_data() for row in self._button_rows],
                    "axes": [row.to_data() for row in self._axis_rows],
                }
            )
        except VirtualDeviceConfigError as exc:
            self._status.set_text(str(exc))
            return
        if self._on_save(template):
            self.close()
        else:
            self._status.set_text(
                "That template ID is already in use. Choose another ID under Device identity."
            )


class VirtualDeviceInstanceDialog(Adw.Dialog):
    def __init__(
        self,
        instance: VirtualDeviceInstance | None,
        templates: Sequence[VirtualDeviceTemplate],
        on_save: Callable[[VirtualDeviceInstance], bool],
        *,
        creating: bool = False,
    ) -> None:
        super().__init__(
            title="Edit output" if instance and not creating else "Add output",
            content_width=520,
            content_height=540,
        )
        self._on_save = on_save
        self._template_ids = [template.id for template in templates]

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Device")
        page.add(group)
        values = instance_to_data(instance) if instance else {}
        self._output_row, _ = _entry_row("Output ID", str(values.get("output_id", "")))
        group.add(self._output_row)

        template_row = Adw.ComboRow(title="Template")
        template_row.set_model(Gtk.StringList.new([template.label for template in templates]))
        selected_template = str(values.get("template", ""))
        if selected_template in self._template_ids:
            template_row.set_selected(self._template_ids.index(selected_template))
        self._template_row = template_row
        group.add(template_row)

        self._name_row, _ = _entry_row("Device name override", str(values.get("name", "")))
        self._vendor_row, _ = _entry_row("Vendor ID override", str(values.get("vendor_id", "")))
        self._product_row, _ = _entry_row("Product ID override", str(values.get("product_id", "")))
        self._version_row, _ = _entry_row("Version override", str(values.get("version", "")))
        self._bustype_row, _ = _entry_row("Bus type override", str(values.get("bustype", "")))
        overrides = Adw.ExpanderRow(
            title="Identity overrides", subtitle="Leave empty to use the template's identity"
        )
        group.add(overrides)
        for row in (
            self._name_row,
            self._vendor_row,
            self._product_row,
            self._version_row,
            self._bustype_row,
        ):
            overrides.add_row(row)

        footer = Gtk.ActionBar()
        self._status = Gtk.Label(xalign=0, hexpand=True)
        self._status.add_css_class("error")
        footer.pack_start(self._status)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", self._close_clicked)
        footer.pack_end(cancel)
        save = Gtk.Button(label="Use changes")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._save)
        footer.pack_end(save)
        toolbar.set_content(page)
        toolbar.add_bottom_bar(footer)
        self.set_child(toolbar)

    def _close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _save(self, _button: Gtk.Button) -> None:
        selected = int(self._template_row.get_selected())
        if not 0 <= selected < len(self._template_ids):
            self._status.set_text("Select a template")
            return
        data: dict[str, object] = {
            "output_id": self._output_row.get_text(),
            "template": self._template_ids[selected],
        }
        for key, row in (
            ("name", self._name_row),
            ("vendor_id", self._vendor_row),
            ("product_id", self._product_row),
            ("version", self._version_row),
            ("bustype", self._bustype_row),
        ):
            if row.get_text().strip():
                data[key] = row.get_text().strip()
        try:
            instance = instance_from_data(data)
        except VirtualDeviceConfigError as exc:
            self._status.set_text(str(exc))
            return
        if self._on_save(instance):
            self.close()
        else:
            self._status.set_text("Output ID is already used or reserved. Choose a different ID.")


class VirtualDevicesDialog(Adw.Dialog):
    def __init__(self, parent: Gtk.Widget | None = None) -> None:
        super().__init__(title="Custom virtual devices", content_width=760, content_height=660)
        self._parent = parent
        self._config = load_virtual_device_config()
        self._dirty = False
        self._applying = False
        self.set_can_close(False)
        self.connect("close-attempt", self._on_close_attempt)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._content = content
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        scrolled.set_child(content)

        introduction = Gtk.Label(
            label=(
                "Templates describe a controller. Add an output to make it available "
                "to games and mappings."
            ),
            xalign=0,
            wrap=True,
        )
        content.append(introduction)

        templates_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        templates_header.append(Gtk.Label(label="Templates", xalign=0, hexpand=True))
        add_template = Gtk.Button(label="New template")
        add_template.connect("clicked", self._new_template)
        templates_header.append(add_template)
        content.append(templates_header)
        self._templates_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._templates_list.add_css_class("boxed-list")
        content.append(self._templates_list)

        devices_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        devices_header.append(Gtk.Label(label="Configured outputs", xalign=0, hexpand=True))
        self._add_device_button = Gtk.Button(label="Add output")
        self._add_device_button.connect("clicked", self._new_device)
        devices_header.append(self._add_device_button)
        content.append(devices_header)
        self._devices_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._devices_list.add_css_class("boxed-list")
        content.append(self._devices_list)
        self._empty_outputs = Gtk.Label(
            label=(
                "No additional outputs. Choose Add output on a template to create one.\n"
                "The standard virtual gamepad count is managed in Settings."
            ),
            xalign=0,
            wrap=True,
        )
        self._empty_outputs.add_css_class("dim-label")
        content.append(self._empty_outputs)

        note = Gtk.Label(
            label=(
                "Applying changes reconnects affected virtual devices. Close games that are "
                "currently reading them first."
            ),
            xalign=0,
            wrap=True,
        )
        note.add_css_class("dim-label")
        content.append(note)

        footer = Gtk.ActionBar()
        self._status = Gtk.Label(xalign=0, hexpand=True)
        footer.pack_start(self._status)
        close = Gtk.Button(label="Close")
        close.connect("clicked", self._close_clicked)
        footer.pack_end(close)
        self._apply_button = Gtk.Button(label="Apply")
        self._apply_button.add_css_class("suggested-action")
        self._apply_button.connect("clicked", self._apply)
        footer.pack_end(self._apply_button)
        toolbar.set_content(scrolled)
        toolbar.add_bottom_bar(footer)
        self.set_child(toolbar)
        self._rebuild()

        session_request_async(
            {"command": "get_virtual_devices"},
            self._on_loaded,
            timeout=1.0,
        )

    def _close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _on_close_attempt(self, _dialog: Adw.Dialog) -> None:
        if self._applying:
            return
        if not self._dirty:
            self.force_close()
            return
        confirmation = Adw.AlertDialog(
            heading="Discard unapplied changes?",
            body="Your template and output changes have not been applied.",
        )
        confirmation.add_response("cancel", "Keep editing")
        confirmation.add_response("discard", "Discard")
        confirmation.set_default_response("cancel")
        confirmation.set_close_response("cancel")
        confirmation.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        confirmation.connect("response", self._discard_response)
        confirmation.present(self)

    def _discard_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response == "discard":
            self.force_close()

    def _catalog(self) -> tuple[VirtualDeviceTemplate, ...]:
        return (*BUILTIN_VIRTUAL_DEVICE_TEMPLATES, *self._config.templates)

    def _clear_list(self, list_box: Gtk.ListBox) -> None:
        child = list_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            list_box.remove(child)
            child = next_child

    def _rebuild(self) -> None:
        self._clear_list(self._templates_list)
        for template in BUILTIN_VIRTUAL_DEVICE_TEMPLATES:
            self._templates_list.append(self._template_row(template, builtin=True))
        for index, template in enumerate(self._config.templates):
            self._templates_list.append(self._template_row(template, index=index))

        self._empty_outputs.set_visible(not self._config.devices)
        self._clear_list(self._devices_list)
        catalog = {template.id: template for template in self._catalog()}
        for index, device in enumerate(self._config.devices):
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            box.set_margin_start(12)
            box.set_margin_end(8)
            template = catalog.get(device.template_id)
            label = Gtk.Label(
                label=(f"{device.output_id}\n{template.label if template else device.template_id}"),
                xalign=0,
                hexpand=True,
                tooltip_text=template.label if template else device.template_id,
            )
            box.append(label)
            edit = Gtk.Button(icon_name="document-edit-symbolic")
            edit.set_tooltip_text("Edit output")
            edit.connect("clicked", self._edit_device, index)
            box.append(edit)
            remove = Gtk.Button(icon_name="user-trash-symbolic")
            remove.set_tooltip_text("Remove output")
            remove.connect("clicked", self._remove_device, index)
            box.append(remove)
            row.set_child(box)
            self._devices_list.append(row)
        self._add_device_button.set_sensitive(len(self._config.devices) < MAX_USER_VIRTUAL_DEVICES)
        self._apply_button.set_sensitive(self._dirty and not self._applying)

    def _template_row(
        self,
        template: VirtualDeviceTemplate,
        *,
        builtin: bool = False,
        index: int = -1,
    ) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(8)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        text.append(Gtk.Label(label=template.label, xalign=0, wrap=True, max_width_chars=30))
        detail = Gtk.Label(
            label=(
                f"{'Built-in · ' if builtin else 'Custom · '}"
                f"{len(template.buttons)} buttons, {len(template.axes)} axes"
            ),
            xalign=0,
        )
        detail.add_css_class("dim-label")
        text.append(detail)
        description = {
            XBOX_360_TEMPLATE_ID: "Two sticks, two triggers, and directional controls",
            LOGITECH_EXTREME_3D_TEMPLATE_ID: "Twist, throttle, hat switch, and 12 buttons",
        }.get(template.id)
        if description:
            summary = Gtk.Label(label=description, xalign=0, wrap=True, max_width_chars=30)
            summary.add_css_class("dim-label")
            text.append(summary)
        box.append(text)
        use = Gtk.Button(label="Add output", valign=Gtk.Align.CENTER)
        use.set_sensitive(len(self._config.devices) < MAX_USER_VIRTUAL_DEVICES)
        use.connect("clicked", self._use_template, template)
        box.append(use)
        duplicate = Gtk.Button(
            label="Customize" if builtin else "Duplicate", valign=Gtk.Align.CENTER
        )
        duplicate.connect("clicked", self._duplicate_template, template)
        box.append(duplicate)
        if not builtin:
            edit = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER)
            edit.set_tooltip_text("Edit template")
            edit.connect("clicked", self._edit_template, index)
            box.append(edit)
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            used = any(device.template_id == template.id for device in self._config.devices)
            remove.set_sensitive(not used)
            remove.set_tooltip_text(
                "Remove outputs using this template first" if used else "Remove template"
            )
            remove.connect("clicked", self._remove_template, index)
            box.append(remove)
        row.set_child(box)
        return row

    def _on_loaded(self, response: dict[str, object] | None) -> bool:
        if self._dirty:
            return False
        if isinstance(response, dict) and response.get("status") == "ok":
            try:
                self._config = config_from_json(response.get("config", {}))
            except VirtualDeviceConfigError as exc:
                self._status.set_text(str(exc))
                return False
            self._rebuild()
        return False

    def _present_template_editor(
        self, template: VirtualDeviceTemplate | None, *, creating: bool = False
    ) -> None:
        def save(updated: VirtualDeviceTemplate) -> bool:
            templates = list(self._config.templates)
            if not creating and template is not None and template in templates:
                templates[templates.index(template)] = updated
            else:
                templates.append(updated)
            return self._set_config(templates=templates)

        VirtualTemplateEditorDialog(template, save, creating=creating).present(self)

    def _new_template(self, _button: Gtk.Button) -> None:
        dialog = Adw.AlertDialog(
            heading="Choose a starting layout",
            body="Start with a familiar controller, then rename, remove, or add controls.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("gamepad", "Gamepad")
        dialog.add_response("flight-stick", "Flight stick")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._new_template_layout)
        dialog.present(self)

    def _new_template_layout(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response not in {"gamepad", "flight-stick"}:
            return
        template = next(
            item for item in BUILTIN_VIRTUAL_DEVICE_TEMPLATES if item.layout == response
        )
        copy = unique_template_copy(template, self._catalog())
        self._present_template_editor(copy, creating=True)

    def _duplicate_template(
        self,
        _button: Gtk.Button,
        template: VirtualDeviceTemplate,
    ) -> None:
        copy = unique_template_copy(template, self._catalog())
        self._present_template_editor(copy, creating=True)

    def _edit_template(self, _button: Gtk.Button, index: int) -> None:
        self._present_template_editor(self._config.templates[index])

    def _remove_template(self, _button: Gtk.Button, index: int) -> None:
        template = self._config.templates[index]
        if any(device.template_id == template.id for device in self._config.devices):
            self._status.set_text("Remove outputs using this template first")
            return
        templates = list(self._config.templates)
        templates.pop(index)
        self._set_config(templates=templates)

    def _use_template(self, _button: Gtk.Button, template: VirtualDeviceTemplate) -> None:
        used = {device.output_id for device in self._config.devices}
        base = {
            XBOX_360_TEMPLATE_ID: "standard-gamepad",
            LOGITECH_EXTREME_3D_TEMPLATE_ID: "flight-stick",
        }.get(template.id, template.id[:54])
        output_id = base
        index = 2
        while output_id in used:
            output_id = f"{base}-{index}"
            index += 1
        self._present_device_editor(VirtualDeviceInstance(output_id, template.id), creating=True)

    def _present_device_editor(
        self, instance: VirtualDeviceInstance | None, *, creating: bool = False
    ) -> None:
        def save(updated: VirtualDeviceInstance) -> bool:
            devices = list(self._config.devices)
            if not creating and instance is not None and instance in devices:
                devices[devices.index(instance)] = updated
            else:
                devices.append(updated)
            return self._set_config(devices=devices)

        VirtualDeviceInstanceDialog(instance, self._catalog(), save, creating=creating).present(
            self
        )

    def _new_device(self, _button: Gtk.Button) -> None:
        self._present_device_editor(None)

    def _edit_device(self, _button: Gtk.Button, index: int) -> None:
        self._present_device_editor(self._config.devices[index])

    def _remove_device(self, _button: Gtk.Button, index: int) -> None:
        devices = list(self._config.devices)
        devices.pop(index)
        self._set_config(devices=devices)

    def _set_config(
        self,
        *,
        templates: Sequence[VirtualDeviceTemplate] | None = None,
        devices: Sequence[VirtualDeviceInstance] | None = None,
    ) -> bool:
        candidate = VirtualDeviceConfig(
            templates=tuple(templates if templates is not None else self._config.templates),
            devices=tuple(devices if devices is not None else self._config.devices),
        )
        try:
            candidate = config_from_json(config_to_json(candidate))
        except VirtualDeviceConfigError as exc:
            self._status.set_text(str(exc))
            return False
        self._config = candidate
        self._dirty = True
        self._status.set_text("Unapplied changes")
        self._rebuild()
        return True

    def _apply(self, _button: Gtk.Button) -> None:
        if self._applying:
            return
        config = self._config
        payload = config_to_json(config)
        self._applying = True
        self._content.set_sensitive(False)
        self._apply_button.set_sensitive(False)
        self._status.set_text("Applying…")

        def finish() -> None:
            self._applying = False
            self._content.set_sensitive(True)
            self._rebuild()

        def reconciled(response: dict[str, object] | None) -> bool:
            confirmed = False
            if isinstance(response, dict) and response.get("status") == "ok":
                try:
                    confirmed = config_from_json(response.get("config")) == config
                except VirtualDeviceConfigError:
                    pass
            self._dirty = not confirmed
            self._status.set_text(
                "Applied. Confirmed with the session service."
                if confirmed
                else "Could not confirm changes. Your edits are kept; "
                "retry when the service responds."
            )
            finish()
            return False

        def applied(response: dict[str, object] | None) -> bool:
            if isinstance(response, dict) and response.get("status") == "ok":
                self._config = config_from_json(response.get("config", payload))
                self._dirty = False
                self._status.set_text(str(response.get("warning") or "Applied"))
                finish()
                return False
            if isinstance(response, dict):
                self._status.set_text(str(response.get("message") or "Failed to apply"))
                finish()
                return False
            self._status.set_text("No response to Apply. Checking the current configuration…")
            session_request_async({"command": "get_virtual_devices"}, reconciled, timeout=2.0)
            return False

        session_request_async(
            {"command": "set_virtual_devices", "config": payload},
            applied,
            timeout=2.0,
        )
