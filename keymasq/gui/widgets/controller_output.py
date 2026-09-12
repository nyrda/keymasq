"""Hardware output selection shared by setup and settings."""

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.virtual_device_templates import ResolvedVirtualDevice, resolve_virtual_devices
from keymasq.gui.session_client import GuiTaskResult, run_gui_task
from keymasq.gui.widgets.gamepad_output_choices import virtual_device_config, virtual_gamepad_count


class ControllerOutputGroup(Adw.PreferencesGroup):
    def __init__(
        self,
        selected: str = "passthrough",
        on_changed: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(title="Controller output")
        self.set_description(
            "Applies to all profiles. Virtual controllers receive matching buttons and axes. "
            "Map unmatched inputs individually."
        )
        self.output_id = selected
        self._on_changed = on_changed
        self._ids = ["passthrough"]
        self.row = Adw.ComboRow(title="Default output")
        self.row.set_use_subtitle(True)
        self.row.set_subtitle_lines(0)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_choice)
        factory.connect("bind", self._bind_choice)
        self.row.set_list_factory(factory)
        self.add(self.row)
        self._populate([])
        self.row.connect("notify::selected", self._selected)
        run_gui_task(self._load, self._loaded)

    @staticmethod
    def _setup_choice(_factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        box = Gtk.Box(spacing=12)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text.set_hexpand(True)
        for secondary in (False, True):
            label = Gtk.Label(xalign=0)
            label.set_wrap(True)
            label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.set_width_chars(28)
            label.set_max_width_chars(38)
            if secondary:
                label.add_css_class("dim-label")
                label.add_css_class("caption")
            text.append(label)
        box.append(text)
        check = Gtk.Image.new_from_icon_name("object-select-symbolic")
        item.bind_property("selected", check, "visible", GObject.BindingFlags.SYNC_CREATE)
        box.append(check)
        item.set_child(box)

    def _bind_choice(self, _factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        box = item.get_child()
        value = item.get_item()
        if not isinstance(box, Gtk.Box) or not isinstance(value, Gtk.StringObject):
            return
        text = box.get_first_child()
        if not isinstance(text, Gtk.Box):
            return
        title = text.get_first_child()
        if not isinstance(title, Gtk.Label):
            return
        detail = title.get_next_sibling()
        if not isinstance(detail, Gtk.Label):
            return
        title.set_text(value.get_string())
        output_id = self._ids[item.get_position()]
        detail.set_text(
            "Keep the original controller layout" if output_id == "passthrough" else output_id
        )

    @staticmethod
    def _load() -> list[ResolvedVirtualDevice]:
        return list(resolve_virtual_devices(virtual_gamepad_count(), virtual_device_config()))

    def _loaded(self, result: GuiTaskResult[list[ResolvedVirtualDevice]]) -> None:
        if result.error is None:
            self.row.handler_block_by_func(self._selected)
            self._populate(result.value or [])
            self.row.handler_unblock_by_func(self._selected)

    def _populate(self, devices: list[ResolvedVirtualDevice]) -> None:
        choices = [("passthrough", "Passthrough")]
        choices.extend(
            (
                device.output_id,
                f"Virtual Gamepad {device.output_id.removeprefix('virtual-gamepad-')}"
                if device.output_id.startswith("virtual-gamepad-")
                else device.name,
            )
            for device in devices
        )
        if all(output_id != self.output_id for output_id, _ in choices):
            choices.append((self.output_id, f"{self.output_id} (unavailable)"))
        self._ids = [output_id for output_id, _ in choices]
        self.row.set_model(Gtk.StringList.new([label for _, label in choices]))
        self.row.set_selected(self._ids.index(self.output_id))

    def _selected(self, _row: Adw.ComboRow, _param: object) -> None:
        index = self.row.get_selected()
        if 0 <= index < len(self._ids):
            self.output_id = self._ids[index]
            if self._on_changed is not None:
                self._on_changed(self.output_id)

    def restore_output(self, output_id: str) -> None:
        self.output_id = output_id
        self.row.handler_block_by_func(self._selected)
        self.row.set_selected(self._ids.index(output_id))
        self.row.handler_unblock_by_func(self._selected)
