from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.models import AnalogInputDefinition, ButtonDefinition


def present_device_rename_dialog(
    *,
    parent,
    current_name: str,
    on_save: Callable[[str], bool],
    on_close_clicked: Callable[[Gtk.Button, Adw.Dialog], None],
) -> None:
    dialog = Adw.Dialog(title="Rename Device", content_width=420, content_height=-1)
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    box = _dialog_box()

    label = Gtk.Label(label=f"Rename '{current_name}'")
    label.set_halign(Gtk.Align.START)
    box.append(label)

    entry = Gtk.Entry()
    entry.set_text(current_name)
    entry.set_activates_default(True)
    box.append(entry)

    content.append(box)
    content.append(Gtk.Separator())

    footer = Gtk.CenterBox(orientation=Gtk.Orientation.HORIZONTAL)
    footer.set_margin_top(6)
    footer.set_margin_bottom(6)
    footer.set_margin_start(12)
    footer.set_margin_end(12)

    btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    btn_row.set_halign(Gtk.Align.END)

    cancel_btn = Gtk.Button(label="Cancel")
    cancel_btn.connect("clicked", on_close_clicked, dialog)
    btn_row.append(cancel_btn)

    save_btn = Gtk.Button(label="Save")
    save_btn.add_css_class("suggested-action")
    save_btn.set_receives_default(True)

    def save(_button) -> None:
        if on_save(entry.get_text()):
            dialog.close()

    save_btn.connect("clicked", save)
    btn_row.append(save_btn)

    footer.set_end_widget(btn_row)
    content.append(footer)
    dialog.set_child(content)
    dialog.present(parent)


def present_button_relabel_dialog(
    *,
    parent,
    button: ButtonDefinition,
    on_delete_clicked: Callable[[Gtk.Button, Adw.Dialog, ButtonDefinition], None],
    on_save: Callable[[ButtonDefinition, str], bool],
    on_close_clicked: Callable[[Gtk.Button, Adw.Dialog], None],
) -> None:
    _present_input_relabel_dialog(
        parent=parent,
        title="Rename Key",
        label=f"Rename '{button.label}'",
        current_label=button.label,
        subject=button,
        on_delete_clicked=on_delete_clicked,
        on_save=on_save,
        on_close_clicked=on_close_clicked,
    )


def present_analog_relabel_dialog(
    *,
    parent,
    analog: AnalogInputDefinition,
    on_delete_clicked: Callable[[Gtk.Button, Adw.Dialog, AnalogInputDefinition], None],
    on_save: Callable[[AnalogInputDefinition, str], bool],
    on_close_clicked: Callable[[Gtk.Button, Adw.Dialog], None],
) -> None:
    _present_input_relabel_dialog(
        parent=parent,
        title="Rename Analog Input",
        label=f"Rename '{analog.label}'",
        current_label=analog.label,
        subject=analog,
        on_delete_clicked=on_delete_clicked,
        on_save=on_save,
        on_close_clicked=on_close_clicked,
    )


def present_delete_device_dialog(
    *,
    parent,
    device_name: str,
    can_delete: bool,
    can_delete_profiles: bool,
    on_confirm_clicked: Callable[[Gtk.Button, Adw.Dialog, Gtk.CheckButton, Gtk.Label], None],
    on_close_clicked: Callable[[Gtk.Button, Adw.Dialog], None],
) -> None:
    dialog = Adw.Dialog(title="Delete Device", content_width=360, content_height=-1)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    content.set_margin_top(20)
    content.set_margin_bottom(20)
    content.set_margin_start(20)
    content.set_margin_end(20)

    message = Gtk.Label(label=f"Delete device '{device_name}'?\nThis cannot be undone.")
    message.set_halign(Gtk.Align.START)
    content.append(message)

    error_label = Gtk.Label()
    error_label.add_css_class("error")
    error_label.set_halign(Gtk.Align.START)
    error_label.set_wrap(True)
    error_label.set_visible(False)
    content.append(error_label)

    delete_profiles_check = Gtk.CheckButton(label="Remove device mappings from profiles")
    delete_profiles_check.set_active(can_delete_profiles)
    delete_profiles_check.set_sensitive(can_delete_profiles)
    content.append(delete_profiles_check)

    btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    btn_box.set_halign(Gtk.Align.END)
    btn_box.set_margin_top(8)

    cancel_btn = Gtk.Button(label="Cancel")
    cancel_btn.connect("clicked", on_close_clicked, dialog)
    btn_box.append(cancel_btn)

    delete_btn = Gtk.Button(label="Delete")
    delete_btn.add_css_class("destructive-action")
    delete_btn.set_sensitive(can_delete)
    delete_btn.connect("clicked", on_confirm_clicked, dialog, delete_profiles_check, error_label)
    btn_box.append(delete_btn)
    content.append(btn_box)

    if not can_delete:
        error_label.set_label("Action unavailable: missing hardware manager.")
        error_label.set_visible(True)

    dialog.set_child(content)
    dialog.present(parent)


def _present_input_relabel_dialog(
    *,
    parent,
    title: str,
    label: str,
    current_label: str,
    subject: Any,
    on_delete_clicked: Callable[..., None],
    on_save: Callable[..., bool],
    on_close_clicked: Callable[[Gtk.Button, Adw.Dialog], None],
) -> None:
    dialog = Adw.Dialog(title=title, content_width=420, content_height=-1)
    box = _dialog_box()

    title_label = Gtk.Label(label=label)
    title_label.set_halign(Gtk.Align.START)
    box.append(title_label)

    entry = Gtk.Entry()
    entry.set_text(current_label)
    entry.set_activates_default(True)
    box.append(entry)

    btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    btn_row.set_halign(Gtk.Align.FILL)

    delete_btn = Gtk.Button(label="Delete")
    delete_btn.add_css_class("destructive-action")
    delete_btn.connect("clicked", on_delete_clicked, dialog, subject)
    btn_row.append(delete_btn)

    spacer = Gtk.Box()
    spacer.set_hexpand(True)
    btn_row.append(spacer)

    cancel_btn = Gtk.Button(label="Cancel")
    cancel_btn.connect("clicked", on_close_clicked, dialog)
    btn_row.append(cancel_btn)

    save_btn = Gtk.Button(label="Save")
    save_btn.add_css_class("suggested-action")
    save_btn.set_receives_default(True)

    def save(_button) -> None:
        if on_save(subject, entry.get_text()):
            dialog.close()

    save_btn.connect("clicked", save)
    btn_row.append(save_btn)

    box.append(btn_row)
    dialog.set_child(box)
    dialog.present(parent)


def _dialog_box() -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    box.set_margin_top(16)
    box.set_margin_bottom(16)
    box.set_margin_start(16)
    box.set_margin_end(16)
    return box
