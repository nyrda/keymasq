import json
from dataclasses import dataclass
from datetime import datetime
from typing import cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.coercion import coerce_int
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.profiles import ComboConfig, ComboEvent, ComboStep
from keymasq.gui.icons import combo_icon_names, image_from_icon_names
from keymasq.gui.session_client import (
    JsonDict,
    register_session_event_callback,
    session_request_async,
    unregister_session_event_callback,
)
from keymasq.gui.widgets.action_labels import describe_mapping_action_compact
from keymasq.gui.widgets.action_payloads import mapping_action_from_payload
from keymasq.gui.widgets.combo_list import SORT_ACTION, SORT_NAME, SORT_TRIGGER, SortableComboList
from keymasq.gui.widgets.combo_presentation import (
    ComboSearchDocument,
    combo_action_label,
    combo_default_name,
    combo_key_label,
    combo_row_search_matches,
    combo_search_document,
    combo_trigger_label,
    create_combo_summary_row,
)
from keymasq.gui.widgets.fuzzy_search import start_search_from_keypress


@dataclass
class ComboInspectorItem:
    combo_id: str
    name: str
    profile_name: str
    steps: list[ComboStep]
    action: MappingAction | None
    order: int
    recall_trigger_keys: bool = False
    restore_trigger_keys: list[str] | None = None
    match_across_devices: bool = False
    step_tooltips: list[str] | None = None
    search_document: ComboSearchDocument = ComboSearchDocument()


@dataclass
class ComboInspectorSnapshot:
    signature: str
    seen_at: str
    active_profiles: list[str]
    items: list[ComboInspectorItem]


SNAPSHOT_HISTORY_LIMIT = 12


class ComboInspectorWindow(Adw.Window):
    def __init__(self, parent: Gtk.Window):
        super().__init__()
        self._parent = parent
        self._closing = False
        self._finalized = False
        self._items: list[ComboInspectorItem] = []
        self._active_profiles: list[str] = []
        self._snapshots: list[ComboInspectorSnapshot] = []
        self._latest_snapshot_signature = ""
        self._selected_snapshot_signature = ""
        self._follow_latest_snapshot = True
        self._syncing_snapshot_selector = False
        self._snapshot_request_counter = 0

        self.set_title("Inspect Active Combos")
        self.set_default_size(780, 520)
        self.set_transient_for(parent)
        self.set_modal(False)

        self._build_ui()

        register_session_event_callback("profiles_changed", self._on_profiles_changed)
        register_session_event_callback("runtime_reset", self._on_runtime_reset)
        register_session_event_callback("keymasqd_status", self._on_keymasqd_status)
        self.connect("close-request", self._on_close_request)
        self.connect("destroy", self._on_destroy)

        self._request_snapshot()

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        icon = image_from_icon_names(*combo_icon_names(), pixel_size=24)
        icon.set_valign(Gtk.Align.CENTER)
        header.pack_start(icon)

        self._status_label = Gtk.Label(label="Loading active combos")
        self._status_label.add_css_class("inspector-header-title")
        self._status_label.set_halign(Gtk.Align.START)
        self._status_label.set_hexpand(True)
        self._status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._status_label.set_max_width_chars(54)
        header.pack_start(self._status_label)
        header.set_title_widget(Gtk.Box())

        self.snapshot_dropdown = Gtk.DropDown()
        self.snapshot_dropdown.set_tooltip_text("Select a captured active-combo snapshot")
        self.snapshot_dropdown.set_size_request(230, -1)
        self.snapshot_dropdown.connect("notify::selected", self._on_snapshot_selected)
        header.pack_end(self.snapshot_dropdown)

        self.search_button = Gtk.Button()
        self.search_button.set_icon_name("system-search-symbolic")
        self.search_button.set_tooltip_text("Search active combos")
        self.search_button.connect("clicked", self._on_search_clicked)
        header.pack_end(self.search_button)

        toolbar.add_top_bar(header)

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_top(14)
        content.set_margin_bottom(14)
        content.set_margin_start(14)
        content.set_margin_end(14)

        active_profile_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        active_title = Gtk.Label(label="Active profiles:")
        active_title.add_css_class("caption")
        active_title.add_css_class("dim-label")
        active_profile_box.append(active_title)

        self.active_profiles_label = Gtk.Label(label="None")
        self.active_profiles_label.add_css_class("caption")
        self.active_profiles_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.active_profiles_label.set_hexpand(True)
        self.active_profiles_label.set_halign(Gtk.Align.START)
        active_profile_box.append(self.active_profiles_label)
        content.append(active_profile_box)

        self._combo_list = SortableComboList[ComboInspectorItem](
            search_placeholder="Search active combos",
            search_tooltip=(
                "Filter active combos by name, trigger, action, profile, device, or source"
            ),
            empty_text="No active combos.",
            no_match_text="No matching active combos.",
            get_items=lambda: self._items,
            sort_keys={
                SORT_NAME: lambda item: item.name,
                SORT_TRIGGER: lambda item: combo_trigger_label(item.steps),
                SORT_ACTION: lambda item: describe_mapping_action_compact(item.action),
            },
            create_row=self._create_combo_row,
            search_matches=combo_row_search_matches,
        )
        self.search_entry = self._combo_list.search_entry
        self.section_label = self._combo_list.section_label
        self.column_header = self._combo_list.column_header
        self.combo_listbox = self._combo_list.listbox
        self._name_header_btn = self._combo_list.name_header_btn
        self._trigger_header_btn = self._combo_list.trigger_header_btn
        self._action_header_btn = self._combo_list.action_header_btn

        content.append(self.search_entry)

        content.append(self.section_label)

        content.append(self.column_header)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(self.combo_listbox)
        content.append(scrolled)

        toolbar.set_content(content)
        self.set_content(toolbar)

    def _request_snapshot(self) -> None:
        if self._closing:
            return
        self._snapshot_request_counter += 1
        request_id = self._snapshot_request_counter
        session_request_async(
            {"command": "get_combo_inspector_snapshot"},
            lambda result: self._on_snapshot_response(result, request_id),
            timeout=3.0,
        )

    def _on_snapshot_response(self, result: JsonDict | None, request_id: int) -> bool:
        if self._closing or request_id != self._snapshot_request_counter:
            return False
        if not result or result.get("status") != "ok":
            message = _text((result or {}).get("message"), "Combo inspector unavailable")
            self._set_status_title(message, "stopped")
            self._items = []
            self._active_profiles = []
            self._sync_active_profile_label()
            self._combo_list.render()
            return False
        self._apply_snapshot(result)
        return False

    def _apply_snapshot(self, snapshot: JsonDict) -> None:
        new_snapshot = _snapshot_from_payload(snapshot)
        self._store_snapshot(new_snapshot)
        selected = self._selected_snapshot()
        if selected is not None:
            self._show_snapshot(selected)

    def _store_snapshot(self, snapshot: ComboInspectorSnapshot) -> None:
        self._latest_snapshot_signature = snapshot.signature
        self._snapshots = [
            existing for existing in self._snapshots if existing.signature != snapshot.signature
        ]
        self._snapshots.insert(0, snapshot)
        del self._snapshots[SNAPSHOT_HISTORY_LIMIT:]

        selected_still_exists = any(
            existing.signature == self._selected_snapshot_signature for existing in self._snapshots
        )
        if not selected_still_exists:
            self._follow_latest_snapshot = True

        if self._follow_latest_snapshot or not self._selected_snapshot_signature:
            self._selected_snapshot_signature = snapshot.signature
            self._follow_latest_snapshot = True
        elif self._selected_snapshot_signature == snapshot.signature:
            self._follow_latest_snapshot = True

        self._refresh_snapshot_dropdown()

    def _selected_snapshot(self) -> ComboInspectorSnapshot | None:
        for snapshot in self._snapshots:
            if snapshot.signature == self._selected_snapshot_signature:
                return snapshot
        return self._snapshots[0] if self._snapshots else None

    def _show_snapshot(self, snapshot: ComboInspectorSnapshot) -> None:
        self._selected_snapshot_signature = snapshot.signature
        self._follow_latest_snapshot = snapshot.signature == self._latest_snapshot_signature
        self._active_profiles = list(snapshot.active_profiles)
        self._items = list(snapshot.items)
        self._sync_active_profile_label()
        count = len(self._items)
        suffix = "combo" if count == 1 else "combos"
        if self._follow_latest_snapshot:
            title = f"Active Combos - {count} {suffix}"
        else:
            title = f"Active Combos Snapshot - {count} {suffix}"
        self._set_status_title(title, "monitoring")
        self._combo_list.render()

    def _refresh_snapshot_dropdown(self) -> None:
        strings = Gtk.StringList()
        for snapshot in self._snapshots:
            strings.append(self._snapshot_dropdown_label(snapshot))

        selected_index = 0
        for index, snapshot in enumerate(self._snapshots):
            if snapshot.signature == self._selected_snapshot_signature:
                selected_index = index
                break

        self._syncing_snapshot_selector = True
        try:
            self.snapshot_dropdown.set_model(strings)
            self.snapshot_dropdown.set_sensitive(len(self._snapshots) > 1)
            if self._snapshots:
                self.snapshot_dropdown.set_selected(selected_index)
        finally:
            self._syncing_snapshot_selector = False

    def _snapshot_dropdown_label(self, snapshot: ComboInspectorSnapshot) -> str:
        prefix = (
            "Now" if snapshot.signature == self._latest_snapshot_signature else snapshot.seen_at
        )
        summary = _profile_summary(snapshot.active_profiles)
        count = len(snapshot.items)
        suffix = "combo" if count == 1 else "combos"
        return f"{prefix}: {summary} ({count} {suffix})"

    def _on_snapshot_selected(self, dropdown: Gtk.DropDown, _param: object) -> None:
        if self._syncing_snapshot_selector:
            return
        selected = dropdown.get_selected()
        if selected >= len(self._snapshots):
            return
        self._show_snapshot(self._snapshots[selected])

    def _sync_active_profile_label(self) -> None:
        if not self._active_profiles:
            self.active_profiles_label.set_text("None")
            self.active_profiles_label.set_tooltip_text("No profiles are active.")
            return
        visible_names = self._active_profiles[:3]
        summary = ", ".join(visible_names)
        if len(self._active_profiles) > len(visible_names):
            summary += f", +{len(self._active_profiles) - len(visible_names)}"
        self.active_profiles_label.set_text(summary)
        self.active_profiles_label.set_tooltip_text(
            "Layer order: " + " -> ".join(self._active_profiles)
        )

    def _set_status_title(self, text: str, state: str) -> None:
        self._status_label.set_text(text)
        self._status_label.set_tooltip_text(text)
        for css_class in (
            "inspector-header-monitoring",
            "inspector-header-suppressed",
            "inspector-header-stopped",
        ):
            self._status_label.remove_css_class(css_class)
        self._status_label.add_css_class(
            {
                "stopped": "inspector-header-stopped",
            }.get(state, "inspector-header-monitoring")
        )

    def _create_combo_row(self, item: ComboInspectorItem) -> Gtk.ListBoxRow:
        row = create_combo_summary_row(
            name=item.name,
            subtitle=f"Profile: {item.profile_name or '?'}",
            steps=item.steps,
            action=item.action,
            read_only=True,
            tooltip=_combo_tooltip(item),
            step_tooltips=item.step_tooltips,
        )
        row._combo_id = item.combo_id  # type: ignore[attr-defined]
        row._combo_search_document = item.search_document  # type: ignore[attr-defined]
        return row

    def _on_search_clicked(self, _button: Gtk.Button) -> None:
        self._combo_list.show_search()

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if keyval in (Gdk.KEY_f, Gdk.KEY_F) and state & Gdk.ModifierType.CONTROL_MASK:
            self._combo_list.show_search()
            return True
        if keyval == Gdk.KEY_Escape and self.search_entry.get_visible():
            self._combo_list.hide_search()
            return True
        return start_search_from_keypress(
            self,
            self.search_entry,
            keyval,
            state,
            show_search=self._combo_list.show_search,
        )

    def _on_profiles_changed(self, _event: JsonDict) -> bool:
        self._request_snapshot()
        return False

    def _on_runtime_reset(self, _event: JsonDict) -> bool:
        self._request_snapshot()
        return False

    def _on_keymasqd_status(self, event: JsonDict) -> bool:
        if bool(event.get("connected", False)):
            self._request_snapshot()
        else:
            self._snapshot_request_counter += 1
            self._set_status_title("Active Combos - Daemon disconnected", "stopped")
        return False

    def _on_close_request(self, *_args: object) -> bool:
        self._finalize()
        return False

    def _on_destroy(self, *_args: object) -> None:
        self._finalize()

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        self._closing = True
        unregister_session_event_callback("profiles_changed", self._on_profiles_changed)
        unregister_session_event_callback("runtime_reset", self._on_runtime_reset)
        unregister_session_event_callback("keymasqd_status", self._on_keymasqd_status)


def _snapshot_from_payload(snapshot: JsonDict) -> ComboInspectorSnapshot:
    combos = snapshot.get("combos", [])
    items = [
        item
        for item in (_combo_item_from_payload(raw) for raw in _list_of_dicts(combos))
        if item is not None
    ]
    return ComboInspectorSnapshot(
        signature=_snapshot_signature(snapshot),
        seen_at=datetime.now().strftime("%H:%M:%S"),
        active_profiles=_active_profiles_from_payload(snapshot),
        items=items,
    )


def _snapshot_signature(snapshot: JsonDict) -> str:
    combos: list[dict[str, object]] = []
    for combo_payload in _list_of_dicts(snapshot.get("combos")):
        steps: list[dict[str, object]] = []
        for step_payload in _list_of_dicts(combo_payload.get("steps")):
            events = [
                {
                    "evdev": _text(event_payload.get("evdev")),
                    "hardware_id": _text(event_payload.get("hardware_id")),
                    "source": _text(event_payload.get("source")),
                    "device_name": _text(event_payload.get("device_name")),
                }
                for event_payload in _list_of_dicts(step_payload.get("events"))
            ]
            steps.append(
                {
                    "timeout_ms": coerce_int(step_payload.get("timeout_ms"), None),
                    "events": events,
                }
            )
        combos.append(
            {
                "id": _text(combo_payload.get("id")),
                "name": _text(combo_payload.get("name")),
                "profile_name": _text(combo_payload.get("profile_name")),
                "order": coerce_int(combo_payload.get("order"), 0),
                "steps": steps,
                "action": _json_compatible(combo_payload.get("action")),
                "recall_trigger_keys": bool(combo_payload.get("recall_trigger_keys", False)),
                "restore_trigger_keys": [
                    str(value)
                    for value in cast(
                        list[object],
                        combo_payload.get("restore_trigger_keys") or [],
                    )
                    if str(value or "").strip()
                ],
                "match_across_devices": bool(combo_payload.get("match_across_devices", False)),
            }
        )
    return json.dumps(
        {
            "active_profiles": _active_profiles_from_payload(snapshot),
            "combos": combos,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_compatible(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _json_compatible(item)
            for key, item in cast(dict[object, object], value).items()
        }
    if isinstance(value, list):
        return [_json_compatible(item) for item in cast(list[object], value)]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _active_profiles_from_payload(snapshot: JsonDict) -> list[str]:
    active_profiles = snapshot.get("active_profiles", [])
    if not isinstance(active_profiles, list):
        return []
    return [str(name) for name in active_profiles if str(name or "").strip()]


def _profile_summary(active_profiles: list[str]) -> str:
    if not active_profiles:
        return "No active profiles"
    visible_names = active_profiles[:2]
    summary = " -> ".join(visible_names)
    if len(active_profiles) > len(visible_names):
        summary += f" -> +{len(active_profiles) - len(visible_names)}"
    return summary


def _combo_item_from_payload(payload: JsonDict) -> ComboInspectorItem | None:
    steps: list[ComboStep] = []
    step_tooltips: list[str] = []
    event_search_fields: list[str] = []
    for step_payload in _list_of_dicts(payload.get("steps")):
        events: list[ComboEvent] = []
        scope_lines: list[str] = []
        for event_payload in _list_of_dicts(step_payload.get("events")):
            evdev = _text(event_payload.get("evdev"))
            if not evdev:
                continue
            hardware_id = _text(event_payload.get("hardware_id"))
            source = _text(event_payload.get("source"))
            device_name = _text(event_payload.get("device_name"))
            events.append(ComboEvent(evdev=evdev, hardware_id=hardware_id, source=source or None))
            scope_lines.append(_event_scope_label(evdev, hardware_id, source, device_name))
            if device_name:
                event_search_fields.append(device_name)
                if source:
                    event_search_fields.append(f"{device_name} {source}")
        if not events:
            continue
        timeout_ms = coerce_int(step_payload.get("timeout_ms"), None)
        steps.append(ComboStep(events=events, timeout_ms=timeout_ms))
        step_tooltips.append("\n".join(scope_lines))
    if not steps:
        return None

    action = mapping_action_from_payload(payload.get("action"))
    combo_id = _text(payload.get("id"))
    profile_name = _text(payload.get("profile_name"))
    name = _text(payload.get("name"))
    if not name:
        name = _default_name(steps, action)
    restore_trigger_keys = [
        str(value)
        for value in cast(list[object], payload.get("restore_trigger_keys") or [])
        if str(value or "").strip()
    ]
    recall_trigger_keys = bool(payload.get("recall_trigger_keys", False))
    match_across_devices = bool(payload.get("match_across_devices", False))
    search_config = _search_combo_config(
        combo_id,
        name,
        steps,
        action,
        recall_trigger_keys,
        restore_trigger_keys,
        match_across_devices,
    )
    return ComboInspectorItem(
        combo_id=combo_id,
        name=name,
        profile_name=profile_name,
        steps=steps,
        action=action,
        order=coerce_int(payload.get("order"), 0),
        recall_trigger_keys=recall_trigger_keys,
        restore_trigger_keys=restore_trigger_keys,
        match_across_devices=match_across_devices,
        step_tooltips=step_tooltips,
        search_document=combo_search_document(
            search_config,
            profile_name=profile_name,
            additional_event_fields=event_search_fields,
        ),
    )


def _search_combo_config(
    combo_id: str,
    name: str,
    steps: list[ComboStep],
    action: MappingAction | None,
    recall_trigger_keys: bool,
    restore_trigger_keys: list[str],
    match_across_devices: bool,
) -> ComboConfig:
    return ComboConfig(
        id=combo_id,
        name=name,
        steps=steps,
        action=action,
        recall_trigger_keys=recall_trigger_keys,
        restore_trigger_keys=restore_trigger_keys,
        match_across_devices=match_across_devices,
    )


def _combo_tooltip(item: ComboInspectorItem) -> str:
    lines = [
        f"Profile: {item.profile_name or '?'}",
        f"Trigger: {combo_trigger_label(item.steps) or '?'}",
        f"Action: {describe_mapping_action_compact(item.action)}",
        f"Runtime order: {item.order + 1}",
    ]
    if item.recall_trigger_keys:
        lines.append("Recall trigger keys")
    if item.restore_trigger_keys:
        restore_keys = ", ".join(combo_key_label(key) for key in item.restore_trigger_keys)
        lines.append(f"Restore: {restore_keys}")
    if item.step_tooltips:
        lines.append("")
        lines.extend(item.step_tooltips)
    return "\n".join(lines)


def _event_scope_label(
    evdev: str,
    hardware_id: str,
    source: str,
    device_name: str,
) -> str:
    key = combo_key_label(evdev)
    scope_parts: list[str] = []
    if device_name:
        scope_parts.append(device_name)
    elif hardware_id:
        scope_parts.append(hardware_id)
    else:
        scope_parts.append("Any device")
    if source:
        scope_parts.append(f"source {source}")
    elif not hardware_id:
        scope_parts.append("any source")
    return f"{key}: {', '.join(scope_parts)}"


def _default_name(steps: list[ComboStep], action: MappingAction | None) -> str:
    return combo_default_name(
        ComboConfig(
            id="",
            steps=steps,
            action=action,
            name="",
        )
    ) or combo_action_label(action)


def _list_of_dicts(value: object) -> list[JsonDict]:
    if not isinstance(value, list):
        return []
    return [cast(JsonDict, item) for item in value if isinstance(item, dict)]


def _text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value)
