from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.models import ActionType, MappingAction

type PositionCaptureCallback = Callable[[Gtk.Button, Gtk.Label, Callable[[int, int], None]], None]


@dataclass(frozen=True)
class CompositorActionPreset:
    label: str
    dispatcher: str
    args: str
    hint: str
    captures_position: bool = False


@dataclass(frozen=True)
class CompositorActionDefinition:
    page_id: str
    compositor_id: str
    title: str
    subtitle: str
    dispatcher_placeholder: str
    args_placeholder: str
    action_type: ActionType
    presets: tuple[CompositorActionPreset, ...]
    allow_custom: bool
    is_available: Callable[[MappingAction | None, dict[str, object]], bool]
    extract_fields: Callable[[MappingAction | None], tuple[str, str]]
    build_action: Callable[[str, str], MappingAction]
    describe_action: Callable[[MappingAction], str]


@dataclass(frozen=True)
class CompositorActionPage:
    page_id: str
    title: str
    widget: Gtk.Widget


def build_compositor_dispatch_definition(
    *,
    page_id: str,
    compositor_id: str,
    title: str,
    subtitle: str,
    dispatcher_placeholder: str,
    args_placeholder: str,
    presets: tuple[CompositorActionPreset, ...],
    allow_custom: bool,
    listener_name: str | None = None,
) -> CompositorActionDefinition:
    resolved_listener_name = listener_name or compositor_id

    def is_available(
        current_action: MappingAction | None,
        status: dict[str, object],
    ) -> bool:
        _ = current_action
        return bool(
            status.get("listener_name") == resolved_listener_name
            and status.get("compositor_dispatch_available") is True
        )

    def extract_fields(current_action: MappingAction | None) -> tuple[str, str]:
        if current_action is None or current_action.action_type != ActionType.COMPOSITOR_DISPATCH:
            return "", ""
        current_compositor_id = str(current_action.compositor_id or "").strip()
        if current_compositor_id and current_compositor_id != compositor_id:
            return "", ""
        return (
            str(current_action.compositor_dispatcher or ""),
            str(current_action.compositor_args or ""),
        )

    def build_action(dispatcher: str, args: str) -> MappingAction:
        return MappingAction(
            action_type=ActionType.COMPOSITOR_DISPATCH,
            compositor_id=compositor_id,
            compositor_dispatcher=dispatcher,
            compositor_args=args,
        )

    def describe_action(action: MappingAction) -> str:
        args = str(action.compositor_args or "").strip()
        suffix = f" {args}" if args else ""
        return f"{title} → {action.compositor_dispatcher or '?'}{suffix}"

    return CompositorActionDefinition(
        page_id=page_id,
        compositor_id=compositor_id,
        title=title,
        subtitle=subtitle,
        dispatcher_placeholder=dispatcher_placeholder,
        args_placeholder=args_placeholder,
        action_type=ActionType.COMPOSITOR_DISPATCH,
        presets=presets,
        allow_custom=allow_custom,
        is_available=is_available,
        extract_fields=extract_fields,
        build_action=build_action,
        describe_action=describe_action,
    )


def _definition_matches_action(
    definition: CompositorActionDefinition,
    action: MappingAction | None,
) -> bool:
    if action is None or action.action_type != definition.action_type:
        return False
    compositor_id = str(action.compositor_id or "").strip()
    return bool(compositor_id) and definition.compositor_id == compositor_id


class _CompositorDispatchPage(Gtk.Box):
    def __init__(
        self,
        definition: CompositorActionDefinition,
        current_action: MappingAction | None,
        on_selected: Callable[[MappingAction], None],
        submit_label: str | None = None,
        capture_position: PositionCaptureCallback | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._definition = definition
        self._on_selected = on_selected
        self._submit_label = submit_label
        self._capture_position = capture_position
        self._dispatcher, self._args = definition.extract_fields(current_action)
        self._selecting_initial_preset = False
        self._build_ui()

    def _build_ui(self) -> None:
        self.set_margin_top(16)
        self.set_margin_bottom(16)
        self.set_margin_start(16)
        self.set_margin_end(16)

        title = Gtk.Label(label=self._definition.title)
        title.add_css_class("title-4")
        title.set_halign(Gtk.Align.START)
        self.append(title)

        subtitle = Gtk.Label(label=self._definition.subtitle)
        subtitle.add_css_class("dim-label")
        subtitle.set_wrap(True)
        subtitle.set_halign(Gtk.Align.START)
        self.append(subtitle)

        preset_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        preset_row.set_halign(Gtk.Align.START)

        preset_label = Gtk.Label(label="Preset")
        preset_label.set_size_request(90, -1)
        preset_label.set_halign(Gtk.Align.START)
        preset_row.append(preset_label)

        self._preset_dropdown = Gtk.DropDown()
        preset_model = Gtk.StringList()
        if self._definition.allow_custom:
            preset_model.append("Custom")
        for preset in self._definition.presets:
            preset_model.append(preset.label)
        self._preset_dropdown.set_model(preset_model)
        self._preset_dropdown.set_size_request(280, -1)
        self._preset_dropdown.connect("notify::selected", self._on_preset_changed)
        preset_row.append(self._preset_dropdown)
        self.append(preset_row)

        dispatcher_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dispatcher_row.set_halign(Gtk.Align.START)

        dispatcher_label = Gtk.Label(label="Dispatcher")
        dispatcher_label.set_size_request(90, -1)
        dispatcher_label.set_halign(Gtk.Align.START)
        dispatcher_row.append(dispatcher_label)

        self._dispatcher_entry = Gtk.Entry()
        self._dispatcher_entry.set_hexpand(True)
        self._dispatcher_entry.set_placeholder_text(self._definition.dispatcher_placeholder)
        self._dispatcher_entry.set_text(self._dispatcher)
        self._dispatcher_entry.set_editable(self._definition.allow_custom)
        self._dispatcher_entry.connect("changed", self._on_fields_changed)
        dispatcher_row.append(self._dispatcher_entry)
        self.append(dispatcher_row)

        args_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        args_row.set_halign(Gtk.Align.START)

        args_label = Gtk.Label(label="Arguments")
        args_label.set_size_request(90, -1)
        args_label.set_halign(Gtk.Align.START)
        args_row.append(args_label)

        self._args_entry = Gtk.Entry()
        self._args_entry.set_hexpand(True)
        self._args_entry.set_placeholder_text(self._definition.args_placeholder)
        self._args_entry.set_text(self._args)
        self._args_entry.set_editable(self._definition.allow_custom)
        self._args_entry.connect("changed", self._on_fields_changed)
        args_row.append(self._args_entry)
        self.append(args_row)

        capture_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        capture_row.set_halign(Gtk.Align.START)
        capture_label = Gtk.Label(label="")
        capture_label.set_size_request(90, -1)
        capture_row.append(capture_label)
        self._capture_btn = Gtk.Button(label="Capture")
        self._capture_btn.connect("clicked", self._on_capture_clicked)
        capture_row.append(self._capture_btn)
        self._capture_status = Gtk.Label(label="")
        self._capture_status.add_css_class("dim-label")
        self._capture_status.set_halign(Gtk.Align.START)
        capture_row.append(self._capture_status)
        self._capture_row = capture_row
        self.append(capture_row)

        self._hint_label = Gtk.Label(label="")
        self._hint_label.add_css_class("dim-label")
        self._hint_label.set_wrap(True)
        self._hint_label.set_halign(Gtk.Align.START)
        self.append(self._hint_label)

        self._map_btn = Gtk.Button(
            label=self._submit_label or f"Map {self._definition.title} Action"
        )
        self._map_btn.add_css_class("suggested-action")
        self._map_btn.set_halign(Gtk.Align.START)
        self._map_btn.connect("clicked", self._on_map_clicked)
        self.append(self._map_btn)

        self._select_initial_preset()
        self._update_hint()
        self._update_capture_visibility()
        self._update_map_button()

    def _selected_preset(self) -> CompositorActionPreset | None:
        index = int(self._preset_dropdown.get_selected())
        if self._definition.allow_custom:
            if index <= 0 or index > len(self._definition.presets):
                return None
            return self._definition.presets[index - 1]
        if index < 0 or index >= len(self._definition.presets):
            return None
        return self._definition.presets[index]

    def _apply_selected_preset(self) -> None:
        preset = self._selected_preset()
        if preset is None:
            return
        self._dispatcher_entry.set_text(preset.dispatcher)
        self._args_entry.set_text(preset.args)

    def _select_initial_preset(self) -> None:
        selected = 0
        found = False
        for raw_index, preset in enumerate(self._definition.presets):
            if preset.dispatcher != self._dispatcher:
                continue
            if preset.args != self._args and not preset.captures_position:
                continue
            selected = raw_index if not self._definition.allow_custom else raw_index + 1
            found = True
            break
        self._selecting_initial_preset = True
        try:
            self._preset_dropdown.set_selected(selected)
        finally:
            self._selecting_initial_preset = False
        if not found and not self._definition.allow_custom and self._definition.presets:
            self._apply_selected_preset()

    def _on_preset_changed(self, _dropdown, _pspec) -> None:
        if not self._selecting_initial_preset:
            self._apply_selected_preset()
        self._update_hint()
        self._update_capture_visibility()
        self._update_map_button()

    def _on_fields_changed(self, _entry: Gtk.Entry) -> None:
        self._update_hint()
        self._update_map_button()

    def _update_hint(self) -> None:
        preset = self._selected_preset()
        if preset is not None:
            self._hint_label.set_label(preset.hint)
            return
        dispatcher = self._dispatcher_entry.get_text().strip()
        args = self._args_entry.get_text().strip()
        if not dispatcher:
            self._hint_label.set_label("Choose a preset or enter a dispatcher manually.")
            return
        suffix = f" {args}" if args else ""
        self._hint_label.set_label(
            f"Dispatch '{dispatcher}{suffix}' through {self._definition.title}."
        )

    def _update_capture_visibility(self) -> None:
        preset = self._selected_preset()
        visible = bool(
            preset is not None
            and preset.captures_position
            and self._capture_position is not None
        )
        self._capture_row.set_visible(visible)
        if not visible:
            self._capture_status.set_text("")

    def _update_map_button(self) -> None:
        self._map_btn.set_sensitive(bool(self._dispatcher_entry.get_text().strip()))

    def _on_capture_clicked(self, _btn: Gtk.Button) -> None:
        if self._capture_position is None:
            return
        self._capture_btn.set_sensitive(False)
        self._capture_status.set_text("Capturing...")

        def on_point(x: int, y: int) -> None:
            self._args_entry.set_text(f"{int(x)} {int(y)}")
            self._capture_btn.set_sensitive(True)
            self._capture_status.set_text(f"Captured: {int(x)}, {int(y)}")

        self._capture_position(self._capture_btn, self._capture_status, on_point)

    def _on_map_clicked(self, _btn: Gtk.Button) -> None:
        dispatcher = self._dispatcher_entry.get_text().strip()
        args = self._args_entry.get_text().strip()
        if not dispatcher:
            return
        self._on_selected(self._definition.build_action(dispatcher, args))


def build_compositor_action_pages_for_definitions(
    definitions: Sequence[CompositorActionDefinition],
    current_action: MappingAction | None,
    on_selected: Callable[[MappingAction], None],
    status: Mapping[str, object] | None = None,
    submit_label: str | None = None,
    capture_position: PositionCaptureCallback | None = None,
) -> list[CompositorActionPage]:
    resolved_status = dict(status or {})
    pages: list[CompositorActionPage] = []
    for definition in definitions:
        if (
            not _definition_matches_action(definition, current_action)
            and not definition.is_available(current_action, resolved_status)
        ):
            continue
        pages.append(
            CompositorActionPage(
                page_id=definition.page_id,
                title=definition.title,
                widget=_CompositorDispatchPage(
                    definition,
                    current_action,
                    on_selected,
                    submit_label,
                    capture_position,
                ),
            )
        )
    return pages


def compositor_action_tab_name_for_definitions(
    definitions: Sequence[CompositorActionDefinition],
    action: MappingAction | None,
    status: Mapping[str, object] | None = None,
) -> str | None:
    if action is None:
        return None
    resolved_status = dict(status or {})
    compositor_id = str(action.compositor_id or "").strip()
    if compositor_id:
        for definition in definitions:
            if _definition_matches_action(definition, action):
                return definition.page_id
    for definition in definitions:
        if (
            action.action_type == definition.action_type
            and definition.is_available(action, resolved_status)
        ):
            return definition.page_id
    return None


def describe_compositor_action_for_definitions(
    definitions: Sequence[CompositorActionDefinition],
    action: MappingAction,
) -> str | None:
    compositor_id = str(action.compositor_id or "").strip()
    if compositor_id:
        for definition in definitions:
            if (
                action.action_type == definition.action_type
                and definition.compositor_id == compositor_id
            ):
                return definition.describe_action(action)
        return None
    return None
