#!/usr/bin/env python3
# ruff: noqa: E402

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkX11", "4.0")

from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]
    Gdk,
    GdkX11,
    GLib,
    Graphene,
    Gtk,
)

from keymasq.common.devices import (
    is_by_id_path,
    is_keymasq_device_path,
    make_keymasq_device_path,
)
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.hardware import EvdevDevice, HardwareConfig
from keymasq.gui.application import Application
from keymasq.gui.session_client import session_request
from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog
from keymasq.gui.widgets.combo_editor_dialog import ComboEditorDialog
from keymasq.gui.widgets.device_tab.hardware_settings_dialog import (
    DetectionMethod,
    HardwareSettingsDialog,
)
from keymasq.gui.widgets.gnome_setup_dialog import GnomeSetupDialog
from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog
from keymasq.gui.widgets.macro_editor.dialog import MacroEditorDialog
from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog, TypeMacroDialog
from keymasq.gui.widgets.managed_editor.state import EditorSelection
from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog
from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog
from keymasq.gui.widgets.superkey_editor.dialog import SuperkeyDialog
from keymasq.gui.window import (
    compositor,
    device_tabs,
    macro_recording,
    profiles,
    tab_layout,
)
from keymasq.gui.window.core import MainWindow
from keymasq.gui.wizards.hardware_setup.dialog import HardwareSetupDialog
from keymasq.session.profile.types import ProfileInfo

Json = dict[str, Any]

DEFAULT_COMPOSITOR_STATUS: Json = {
    "compositor_id": "hyprland",
    "listener_name": "hyprland",
    "compositor_dispatch_available": True,
    "supported": True,
}


def _load_manifest(path: Path) -> tuple[Json, list[Json]]:
    with path.open("rb") as f:
        data = tomllib.load(f)
    settings = data.get("settings", {})
    shots = data.get("shot", [])
    if not isinstance(settings, dict):
        settings = {}
    if not isinstance(shots, list):
        raise ValueError("docs screenshot manifest must contain [[shot]] entries")
    return dict(settings), [dict(shot) for shot in shots if isinstance(shot, dict)]


def _safe_name(name: object) -> str:
    value = str(name or "").strip()
    if not value:
        raise ValueError("shot is missing a name")
    return value


def _shot_path(output_root: Path, mode: str, shot: Json) -> Path:
    name = _safe_name(shot.get("name"))
    return output_root / mode / f"{name}.png"


def _image_import_cmd() -> list[str]:
    direct = shutil.which("import")
    if direct:
        return [direct]
    magick = shutil.which("magick")
    if magick:
        return [magick, "import"]
    raise RuntimeError("ImageMagick import is not available")


def _convert_cmd() -> list[str]:
    direct = shutil.which("convert")
    if direct:
        return [direct]
    magick = shutil.which("magick")
    if magick:
        return [magick]
    raise RuntimeError("ImageMagick convert is not available")


def _install_docshot_css() -> None:
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(
        b"""
        window,
        window.background,
        .background,
        .csd,
        .solid-csd,
        .dialog,
        .dialog-content {
          border-radius: 0;
          box-shadow: none;
        }
        .button-card-learn {
          border-style: solid;
        }
        """
    )
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 100,
    )


def _active_window_id() -> str:
    return subprocess.check_output(
        ["xdotool", "getactivewindow"],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def _capture_window(path: Path, window_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [*_image_import_cmd(), "-window", window_id, str(path)],
        check=True,
        capture_output=True,
        text=True,
    )


def _window_geometry(window_id: str) -> tuple[int, int, int, int] | None:
    try:
        output = subprocess.check_output(
            ["xdotool", "getwindowgeometry", "--shell", window_id],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError:
        return None
    values: dict[str, int] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"X", "Y", "WIDTH", "HEIGHT"}:
            try:
                values[key] = int(value)
            except ValueError:
                return None
    if {"X", "Y", "WIDTH", "HEIGHT"} - values.keys():
        return None
    return values["X"], values["Y"], values["WIDTH"], values["HEIGHT"]


def _capture_root_window_area(
    path: Path,
    window_id: str,
    content_size: tuple[int, int] | None = None,
) -> None:
    _capture_window(path, "root")
    geometry = _window_geometry(window_id)
    if geometry is not None:
        _crop_image(path, _clamp_crop_to_image(path, geometry))
    if content_size is None:
        return
    content_width, content_height = content_size
    image_width, image_height = _image_size(path)
    if (
        content_width <= 0
        or content_height <= 0
        or image_width < content_width
        or image_height < content_height
    ):
        return
    crop = _content_crop_for_size(path, content_width, content_height)
    if crop is None:
        x = max(0, (image_width - content_width) // 2)
        y = max(0, (image_height - content_height) // 2)
        crop = (x, y, content_width, content_height)
    _crop_image(path, crop)


def _graphene_rect(bounds: tuple[int, int, int, int]) -> Graphene.Rect:
    x, y, width, height = bounds
    return Graphene.Rect().init(float(x), float(y), float(width), float(height))


def _clamp_bounds_to_widget(
    widget: Gtk.Widget,
    bounds: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    widget_width = widget.get_width()
    widget_height = widget.get_height()
    if widget_width <= 0 or widget_height <= 0:
        raise RuntimeError(f"cannot crop unmapped widget {type(widget).__name__}")

    x, y, width, height = bounds
    x = max(0, min(x, widget_width - 1))
    y = max(0, min(y, widget_height - 1))
    width = max(1, min(width, widget_width - x))
    height = max(1, min(height, widget_height - y))
    return x, y, width, height


def _render_widget_png(
    widget: Gtk.Widget,
    path: Path,
    clip: Graphene.Rect | None = None,
) -> None:
    native = widget.get_native()
    if native is None:
        raise RuntimeError(f"widget {type(widget).__name__} has no native renderer")

    width = widget.get_width()
    height = widget.get_height()
    if width <= 0 or height <= 0:
        raise RuntimeError(f"cannot render unmapped widget {type(widget).__name__}")

    renderer = native.get_renderer()
    node = None
    paintable = Gtk.WidgetPaintable.new(widget)
    for attempt in range(4):
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is not None:
            break
        widget.queue_draw()
        _drain_events()
        if attempt < 3:
            GLib.usleep(50_000)
    if node is None:
        raise RuntimeError(f"widget {type(widget).__name__} produced no render node")

    viewport = clip or Graphene.Rect().init(0.0, 0.0, float(width), float(height))
    texture = renderer.render_texture(node, viewport)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not texture.save_to_png(str(path)):
        raise RuntimeError(f"failed to write rendered widget to {path}")


def _widget_window_id(widget: Gtk.Widget) -> str | None:
    native = widget.get_native()
    if native is None:
        return None
    surface = native.get_surface()
    if surface is None:
        return None
    get_xid = getattr(surface, "get_xid", None)
    if callable(get_xid):
        return str(get_xid())
    try:
        return str(GdkX11.X11Surface.get_xid(surface))
    except (TypeError, AttributeError):
        return None


def _park_pointer() -> None:
    subprocess.run(
        ["xdotool", "mousemove", "1", "1"],
        check=False,
        capture_output=True,
        text=True,
    )


def _normalize_png(path: Path) -> None:
    normalized = path.with_name(f".{path.stem}.normalized{path.suffix}")
    try:
        subprocess.run(
            [
                *_convert_cmd(),
                str(path),
                "-strip",
                "-define",
                "png:exclude-chunk=tIME",
                str(normalized),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        normalized.replace(path)
    finally:
        normalized.unlink(missing_ok=True)


def _crop_image(path: Path, crop: tuple[int, int, int, int]) -> None:
    x, y, width, height = crop
    subprocess.run(
        [
            *_convert_cmd(),
            str(path),
            "-crop",
            f"{width}x{height}+{x}+{y}",
            "+repage",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _clamp_crop_to_image(path: Path, crop: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    image_width, image_height = _image_size(path)
    x, y, width, height = crop
    x = max(0, min(x, image_width - 1))
    y = max(0, min(y, image_height - 1))
    width = max(1, min(width, image_width - x))
    height = max(1, min(height, image_height - y))
    return x, y, width, height


def _image_size(path: Path) -> tuple[int, int]:
    output = subprocess.check_output(
        [*_convert_cmd(), str(path), "-format", "%w %h", "info:"],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()
    width, height = (int(part) for part in output.split())
    return width, height


def _raw_rgba(path: Path) -> tuple[int, int, bytes] | None:
    image_width, image_height = _image_size(path)
    if image_width <= 0 or image_height <= 0:
        return None
    raw = subprocess.check_output(
        [*_convert_cmd(), str(path), "-depth", "8", "rgba:-"],
        stderr=subprocess.DEVNULL,
    )
    expected = image_width * image_height * 4
    if len(raw) < expected:
        return None
    return image_width, image_height, raw


def _content_crop_for_size(
    path: Path,
    content_width: int,
    content_height: int,
) -> tuple[int, int, int, int] | None:
    rgba = _raw_rgba(path)
    if rgba is None:
        return None
    image_width, image_height, raw = rgba
    max_x = image_width - content_width
    max_y = image_height - content_height
    if max_x < 0 or max_y < 0:
        return None

    def rgb_at(x: int, y: int) -> tuple[int, int, int]:
        offset = (y * image_width + x) * 4
        return raw[offset], raw[offset + 1], raw[offset + 2]

    def bad_edge_pixel(x: int, y: int) -> int:
        r, g, b = rgb_at(x, y)
        if max(r, g, b) <= 5:
            return 5
        if g >= r + 28 and b >= r + 36:
            return 5
        if r >= 220 and g >= 220 and b >= 220:
            return 4
        return 0

    x_step = max(1, content_width // 160)
    y_step = max(1, content_height // 160)
    edge_offsets = (0, 4, 8, 12)

    def candidate_score(x: int, y: int) -> tuple[int, int]:
        score = 0
        for offset in edge_offsets:
            if offset >= content_width or offset >= content_height:
                continue
            left = x + offset
            right = x + content_width - 1 - offset
            top = y + offset
            bottom = y + content_height - 1 - offset
            for sample_y in range(y, y + content_height, y_step):
                score += bad_edge_pixel(left, sample_y)
                score += bad_edge_pixel(right, sample_y)
            for sample_x in range(x, x + content_width, x_step):
                score += bad_edge_pixel(sample_x, top)
                score += bad_edge_pixel(sample_x, bottom)
        center_x = max_x // 2
        center_y = max_y // 2
        distance_from_center = abs(x - center_x) + abs(y - center_y)
        return score, distance_from_center

    best: tuple[tuple[int, int], int, int] | None = None
    for y in range(max_y + 1):
        for x in range(max_x + 1):
            score = candidate_score(x, y)
            if best is None or score < best[0]:
                best = (score, x, y)
    if best is None:
        return None
    _, x, y = best
    return x, y, content_width, content_height


def _shot_crop(shot: Json) -> tuple[int, int, int, int] | None:
    raw = shot.get("crop")
    if not isinstance(raw, list) or len(raw) != 4:
        return None
    try:
        x, y, width, height = (int(value) for value in raw)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def _widget_bounds(
    widget: Gtk.Widget,
    root: Gtk.Widget,
    *,
    padding: int = 16,
    padding_top: int | None = None,
    padding_end: int | None = None,
    padding_bottom: int | None = None,
    padding_start: int | None = None,
) -> tuple[int, int, int, int] | None:
    bounds = _raw_widget_bounds(widget, root)
    if bounds is None:
        return None
    return _pad_bounds(
        bounds,
        padding=padding,
        padding_top=padding_top,
        padding_end=padding_end,
        padding_bottom=padding_bottom,
        padding_start=padding_start,
    )


def _raw_widget_bounds(
    widget: Gtk.Widget,
    root: Gtk.Widget,
) -> tuple[int, int, int, int] | None:
    compute_bounds = getattr(widget, "compute_bounds", None)
    if not callable(compute_bounds):
        return None
    try:
        result = compute_bounds(root)
    except TypeError:
        return None

    rect = None
    ok = True
    if isinstance(result, tuple) and len(result) == 2:
        ok, rect = result
    else:
        rect = result
    if not ok or rect is None:
        return None
    try:
        x = int(rect.get_x())
        y = int(rect.get_y())
        width = int(rect.get_width())
        height = int(rect.get_height())
    except AttributeError:
        return None
    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def _pad_bounds(
    bounds: tuple[int, int, int, int],
    *,
    padding: int = 16,
    padding_top: int | None = None,
    padding_end: int | None = None,
    padding_bottom: int | None = None,
    padding_start: int | None = None,
) -> tuple[int, int, int, int]:
    x, y, width, height = bounds
    top = padding if padding_top is None else padding_top
    end = padding if padding_end is None else padding_end
    bottom = padding if padding_bottom is None else padding_bottom
    start = padding if padding_start is None else padding_start
    return (
        max(0, x - start),
        max(0, y - top),
        width + start + end,
        height + top + bottom,
    )


def _iter_widget_tree(widget: Gtk.Widget) -> list[Gtk.Widget]:
    widgets: list[Gtk.Widget] = []
    child = widget.get_first_child()
    while child is not None:
        widgets.append(child)
        widgets.extend(_iter_widget_tree(child))
        child = child.get_next_sibling()
    return widgets


def _dialog_child_widget(dialog: Gtk.Widget) -> Gtk.Widget | None:
    get_child = getattr(dialog, "get_child", None)
    if not callable(get_child):
        return None
    child = get_child()
    return child if isinstance(child, Gtk.Widget) else None


def _dialog_content_bounds(
    dialog: Gtk.Widget,
    root: Gtk.Widget,
    *,
    padding: int = 16,
    padding_top: int | None = None,
    padding_end: int | None = None,
    padding_bottom: int | None = None,
    padding_start: int | None = None,
) -> tuple[int, int, int, int] | None:
    widget = _dialog_child_widget(dialog) or dialog
    return _widget_bounds(
        widget,
        root,
        padding=padding,
        padding_top=padding_top,
        padding_end=padding_end,
        padding_bottom=padding_bottom,
        padding_start=padding_start,
    )


def _scroll_widget_to_bottom(widget: Gtk.Widget) -> bool:
    parent = widget.get_parent()
    while parent is not None:
        if isinstance(parent, Gtk.ScrolledWindow):
            adjustment = parent.get_vadjustment()
            adjustment.set_value(max(0.0, adjustment.get_upper() - adjustment.get_page_size()))
            return False
        parent = parent.get_parent()
    return False


def _stabilize_ancestor_scrollbar(widget: Gtk.Widget) -> None:
    parent = widget.get_parent()
    while parent is not None:
        if isinstance(parent, Gtk.ScrolledWindow):
            parent.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.ALWAYS)
            return
        parent = parent.get_parent()


def _drain_events() -> None:
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def _close_dialog(dialog: object | None) -> None:
    if dialog is None:
        return
    close = getattr(dialog, "close", None)
    if callable(close):
        close()


def _set_dialog_stack(dialog: object, tab_name: object) -> None:
    name = str(tab_name or "").strip()
    stack = getattr(dialog, "stack", None)
    if name and stack is not None and hasattr(stack, "set_visible_child_name"):
        stack.set_visible_child_name(name)
        changed = getattr(dialog, "_on_tab_changed", None)
        if callable(changed):
            changed(stack, None)


def _find_superkey_dialog_row(dialog: SuperkeyDialog, name: str) -> Gtk.ListBoxRow | None:
    return dialog.shell.row_for_selection(EditorSelection.saved_item(name))


def _find_analog_control_dialog_row(
    dialog: AnalogControlDialog,
    name: str,
) -> Gtk.ListBoxRow | None:
    return dialog.shell.row_for_selection(EditorSelection.saved_item(name))


def _find_menu_button_by_label(widget: Gtk.Widget, label: str) -> Gtk.MenuButton | None:
    for descendant in _iter_widget_tree(widget):
        if not isinstance(descendant, Gtk.MenuButton):
            continue
        get_label = getattr(descendant, "get_label", None)
        if callable(get_label) and get_label() == label:
            return descendant
        for child in _iter_widget_tree(descendant):
            if isinstance(child, Gtk.Label) and child.get_label() == label:
                return descendant
    return None


def _expand_expander_row_by_title(widget: Gtk.Widget, title: str) -> bool:
    for descendant in _iter_widget_tree(widget):
        get_title = getattr(descendant, "get_title", None)
        set_expanded = getattr(descendant, "set_expanded", None)
        if callable(get_title) and callable(set_expanded) and get_title() == title:
            set_enable_expansion = getattr(descendant, "set_enable_expansion", None)
            if callable(set_enable_expansion):
                set_enable_expansion(True)
            set_expanded(True)
            return False
    return False


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _apply_dialog_size(dialog: object, shot: Json) -> None:
    width = _optional_int(shot.get("dialog_width"))
    height = _optional_int(shot.get("dialog_height"))
    if width is not None:
        set_width = getattr(dialog, "set_content_width", None)
        if callable(set_width):
            set_width(width)
    if height is not None:
        set_height = getattr(dialog, "set_content_height", None)
        if callable(set_height):
            set_height(height)


def _docshot_recording_devices(devices: object) -> list[Json]:
    raw_devices = devices if isinstance(devices, list) else []
    source_devices = [dict(device) for device in raw_devices if isinstance(device, dict)]
    specs = [
        (
            "keymasq:output:keyboard",
            "DYGMA RAISE2",
            "keyboard",
            "/dev/input/event14",
        ),
        (
            "keymasq:output:mouse",
            "Razer Naga V2 HyperSpeed",
            "mouse",
            "/dev/input/event15",
        ),
        (
            "keymasq:output:gamepad",
            "Xbox 360 1",
            "gamepad",
            "/dev/input/event16",
        ),
    ]
    normalized: list[Json] = []
    for recording_id, name, device_type, path in specs:
        source = next(
            (
                device
                for device in source_devices
                if str(device.get("recording_id", "") or "") == recording_id
            ),
            {},
        )
        device = dict(source)
        device.update(
            {
                "name": name,
                "path": path,
                "stable_path": path,
                "recording_id": recording_id,
                "recording_kind": "keymasq_output",
                "keymasq_output": device_type,
                "device_type": device_type,
                "device_types": [device_type],
                "grabbed_by_keymasq": False,
            }
        )
        normalized.append(device)
    return normalized


def _normalize_record_macro_dialog_devices(dialog: object) -> bool:
    devices = getattr(dialog, "_devices", None)
    if not devices:
        return False
    normalized = _docshot_recording_devices(devices)
    if getattr(dialog, "_devices", None) == normalized:
        return False
    dialog._devices = normalized  # type: ignore[attr-defined]
    populate = getattr(dialog, "_populate_device_list", None)
    if callable(populate):
        populate()
    return False


def _install_record_macro_dialog_fixture(dialog: RecordMacroDialog) -> None:
    attempts = 0

    def normalize_when_loaded() -> bool:
        nonlocal attempts
        attempts += 1
        if bool(getattr(dialog, "_closed", False)):
            return False
        if not getattr(dialog, "_devices", None):
            return attempts < 40
        _normalize_record_macro_dialog_devices(dialog)
        return False

    def schedule_normalization(_dialog: RecordMacroDialog | None = None) -> None:
        GLib.timeout_add(50, normalize_when_loaded)

    dialog.connect("map", schedule_normalization)
    schedule_normalization()


class DocshotRunner:
    def __init__(
        self,
        *,
        app: Application,
        manifest: Path,
        output_root: Path,
        modes: list[str],
    ) -> None:
        self.app = app
        self.manifest = manifest
        self.output_root = output_root
        self.modes = modes
        self.settings, self.shots = _load_manifest(manifest)
        self.window: MainWindow | None = None
        self.mode_index = 0
        self.shot_index = 0
        self.current_dialog: object | None = None
        self.current_popover: Gtk.Popover | None = None
        self.crop_widget: Gtk.Widget | None = None
        self.crop_dialog: Gtk.Widget | None = None
        self.capture_root_window = False
        self.crop_padding = 16
        self.crop_padding_top: int | None = None
        self.crop_padding_end: int | None = None
        self.crop_padding_bottom: int | None = None
        self.crop_padding_start: int | None = None
        self.crop_width: int | None = None
        self.crop_height: int | None = None
        self.failed = False
        self._runtime_overrides_installed = False
        self._orig_apply_compositor_state = None
        self._orig_update_macro_recording_state = None
        self._orig_apply_profile_runtime_state = None
        self._welcome_tabs_isolated = False
        self.runtime_profiles = ["Desktop"]
        self.settle_ms = int(self.settings.get("settle_ms", 700) or 700)
        self.default_width = int(self.settings.get("window_width", 760) or 760)
        self.default_height = int(self.settings.get("window_height", 1000) or 1000)
        self.selector_width = int(self.settings.get("selector_width", 1100) or 1100)
        self.selector_height = int(self.settings.get("selector_height", 900) or 900)

    def start(self) -> bool:
        _install_docshot_css()
        self.window = self.app.window
        if self.window is None or not self.window._startup_probe_done:
            GLib.timeout_add(100, self.start)
            return False
        self._install_runtime_overrides()
        self._apply_runtime_state()
        GLib.idle_add(self._prepare_next)
        return False

    def _docshot_compositor_state(self) -> Json:
        return {
            "compositor_id": "hyprland",
            "support_details": {"supported": True, "warning": ""},
            "supported": True,
            "capabilities": ["dispatch", "keyword"],
        }

    def _docshot_macro_recording_state(self) -> Json:
        return {
            "status": "ok",
            "macro_recording_enabled": True,
            "macro_recording_source": "docshot",
            "macro_recording_expires_at": 4102444800,
        }

    def _docshot_profile_runtime_state(self) -> Json:
        assert self.window is not None
        profiles = [profile for profile in self.runtime_profiles if profile]
        devices: Json = {}
        for hardware_id, page in self.window._device_pages.items():
            child = page.get_child()
            hardware = getattr(child, "device", None)
            if not isinstance(hardware, HardwareConfig):
                hardware = self.window.hardware_manager.get_hardware(hardware_id)
            mapping_count, always_grab_all = self._docshot_profile_device_state(
                hardware_id,
                profiles,
            )
            devices[hardware_id] = {
                "profiles": profiles,
                "mapping_count": mapping_count,
                "always_grab_all": always_grab_all,
                "device_status": self._docshot_device_status(
                    hardware,
                    mapping_count=mapping_count,
                    always_grab_all=always_grab_all,
                ),
            }
        return {
            "status": "ok",
            "active_profiles": profiles,
            "devices": devices,
            "window": {
                "class": "keymasq-docshot",
                "title": "keymasq documentation fixture",
            },
        }

    def _docshot_profile_device_state(
        self,
        hardware_id: str,
        profiles: list[str],
    ) -> tuple[int, bool]:
        assert self.window is not None
        mapping_count = 0
        always_grab_all = False
        for profile_name in profiles:
            profile = self.window.profile_manager.get_profile(profile_name)
            if profile is None:
                continue
            layer = profile.config.get_layer(hardware_id)
            if layer is None:
                continue
            mapping_count += sum(
                1
                for mapping in layer.mappings.values()
                if mapping.action_type != ActionType.PASSTHROUGH
            )
            always_grab_all = always_grab_all or layer.always_grab_all
        return mapping_count, always_grab_all

    def _docshot_device_status(
        self,
        hardware: HardwareConfig | None,
        *,
        mapping_count: int,
        always_grab_all: bool,
    ) -> Json:
        configured = list(hardware.evdev_devices) if hardware is not None else []
        configured_count = len(configured)
        should_grab = mapping_count > 0 or always_grab_all
        connected_count = configured_count
        requested_count = configured_count if should_grab else 0
        grabbed_count = configured_count if should_grab else 0
        state = "grabbed" if should_grab else "connected"
        if configured_count <= 0:
            state = "unknown"
            connected_count = 0
            requested_count = 0
            grabbed_count = 0

        return {
            "state": state,
            "configured_count": configured_count,
            "connected_count": connected_count,
            "requested_count": requested_count,
            "grabbed_count": grabbed_count,
            "interfaces": [
                self._docshot_interface_status(interface, grabbed=should_grab)
                for interface in configured
            ],
            "runtime_ready": True,
            "grab_status": {},
        }

    def _docshot_interface_status(self, interface: object, *, grabbed: bool) -> Json:
        device_type = getattr(interface, "device_type", "")
        device_type_value = getattr(device_type, "value", device_type or "")
        path = str(getattr(interface, "path", "") or "")
        return {
            "id": str(getattr(interface, "id", "") or ""),
            "configured_path": path,
            "type": str(device_type_value),
            "connected": True,
            "requested": grabbed,
            "grabbed": grabbed,
            "current_path": path,
            "stable_path": path,
        }

    def _install_runtime_overrides(self) -> None:
        assert self.window is not None
        if self._runtime_overrides_installed:
            return
        apply_compositor_state = compositor._apply_compositor_state
        update_macro_recording_state = macro_recording._update_macro_recording_state
        apply_profile_runtime_state = profiles._apply_profile_runtime_state
        self._orig_apply_compositor_state = apply_compositor_state
        self._orig_update_macro_recording_state = update_macro_recording_state
        self._orig_apply_profile_runtime_state = apply_profile_runtime_state

        def force_compositor_state(window: MainWindow, _state: Json) -> None:
            apply_compositor_state(window, self._docshot_compositor_state())

        def force_macro_recording_state(window: MainWindow, _state: Json) -> None:
            update_macro_recording_state(window, self._docshot_macro_recording_state())

        def force_profile_runtime_state(window: MainWindow, _state: Json) -> None:
            apply_profile_runtime_state(window, self._docshot_profile_runtime_state())

        compositor._apply_compositor_state = force_compositor_state
        macro_recording._update_macro_recording_state = force_macro_recording_state
        profiles._apply_profile_runtime_state = force_profile_runtime_state
        self._runtime_overrides_installed = True

    def _uninstall_runtime_overrides(self) -> None:
        if not self._runtime_overrides_installed or self.window is None:
            return
        if self._orig_apply_compositor_state is not None:
            compositor._apply_compositor_state = self._orig_apply_compositor_state
        if self._orig_update_macro_recording_state is not None:
            macro_recording._update_macro_recording_state = self._orig_update_macro_recording_state
        if self._orig_apply_profile_runtime_state is not None:
            profiles._apply_profile_runtime_state = self._orig_apply_profile_runtime_state
        self._orig_apply_compositor_state = None
        self._orig_update_macro_recording_state = None
        self._orig_apply_profile_runtime_state = None
        self._runtime_overrides_installed = False

    def _quit(self) -> None:
        self._uninstall_runtime_overrides()
        self.app.quit()

    def _apply_runtime_state(self) -> None:
        assert self.window is not None
        compositor._apply_compositor_state(self.window, self._docshot_compositor_state())
        macro_recording._update_macro_recording_state(
            self.window, self._docshot_macro_recording_state()
        )
        profiles._apply_profile_runtime_state(self.window, self._docshot_profile_runtime_state())

    def _settle_runtime_state(self) -> None:
        if self.window is None:
            return
        self._apply_runtime_state()
        _drain_events()
        self._apply_runtime_state()

    def _runtime_profiles_for_shot(self, shot: Json) -> list[str]:
        target = str(shot.get("target", "") or "")
        if target == "welcome":
            return []
        if target == "device_fresh":
            return ["Default"]
        return [str(shot.get("profile", "Desktop") or "Desktop")]

    def _prepare_next(self) -> bool:
        if self.mode_index >= len(self.modes):
            self._quit()
            return False

        mode = self.modes[self.mode_index]
        if self.shot_index >= len(self.shots):
            self.mode_index += 1
            self.shot_index = 0
            return self._prepare_next()

        self._close_current_dialog()
        shot = self.shots[self.shot_index]
        self.runtime_profiles = self._runtime_profiles_for_shot(shot)
        self.crop_widget = None
        self.crop_dialog = None
        self.capture_root_window = False
        self.crop_padding = 16
        self.crop_padding_top = None
        self.crop_padding_end = None
        self.crop_padding_bottom = None
        self.crop_padding_start = None
        self.crop_width = None
        self.crop_height = None
        target = str(shot.get("target", "") or "")
        self.app.apply_appearance_mode(mode, persist=False)
        self._resize_window(shot)
        if target != "welcome":
            self._restore_fixture_tabs_after_welcome()
            self._close_welcome_placeholder()
        self._apply_runtime_state()

        try:
            self._prepare_shot(shot)
        except Exception as exc:  # noqa: BLE001
            self.failed = True
            print(
                f"Failed to prepare shot {shot.get('name')!r} in {mode}: {exc}",
                file=sys.stderr,
            )
            self._quit()
            return False

        GLib.timeout_add(self.settle_ms, self._capture_current)
        return False

    def _capture_current(self) -> bool:
        mode = self.modes[self.mode_index]
        shot = self.shots[self.shot_index]
        path = _shot_path(self.output_root, mode, shot)
        try:
            _drain_events()
            self._settle_runtime_state()

            if self.capture_root_window:
                self._capture_current_root_window(path, shot)
            else:
                self._render_current(path, shot)
            _normalize_png(path)
        except Exception as exc:  # noqa: BLE001
            self.failed = True
            print(
                f"Failed to capture shot {shot.get('name')!r} in {mode}: {exc}",
                file=sys.stderr,
            )
            self._quit()
            return False

        print(f"captured {mode}/{shot.get('name')}", flush=True)
        self.shot_index += 1
        GLib.idle_add(self._prepare_next)
        return False

    def _capture_current_root_window(self, path: Path, shot: Json) -> None:
        if self.window is None:
            raise RuntimeError("cannot capture root window without a main window")

        _park_pointer()
        _drain_events()
        self._settle_runtime_state()
        window_id = _widget_window_id(self.window) or _active_window_id()
        _capture_root_window_area(
            path,
            window_id,
            (self.window.get_width(), self.window.get_height()),
        )

        crop: tuple[int, int, int, int] | None = None
        if self.crop_dialog is not None:
            crop = _dialog_content_bounds(
                self.crop_dialog,
                self.window,
                padding=self.crop_padding,
                padding_top=self.crop_padding_top,
                padding_end=self.crop_padding_end,
                padding_bottom=self.crop_padding_bottom,
                padding_start=self.crop_padding_start,
            )
        if crop is None and self.crop_widget is not None:
            crop = _widget_bounds(
                self.crop_widget,
                self.window,
                padding=self.crop_padding,
                padding_top=self.crop_padding_top,
                padding_end=self.crop_padding_end,
                padding_bottom=self.crop_padding_bottom,
                padding_start=self.crop_padding_start,
            )
        if crop is None:
            crop = _shot_crop(shot)
        if crop is not None:
            _crop_image(path, _clamp_crop_to_image(path, self._apply_crop_size(crop)))

    def _render_current(self, path: Path, shot: Json) -> None:
        if self.crop_dialog is not None:
            crop = _dialog_content_bounds(
                self.crop_dialog,
                self.crop_dialog,
                padding=self.crop_padding,
                padding_top=self.crop_padding_top,
                padding_end=self.crop_padding_end,
                padding_bottom=self.crop_padding_bottom,
                padding_start=self.crop_padding_start,
            )
            if crop is None:
                raise RuntimeError("dialog content bounds are not available")
            self._render_widget_crop(self.crop_dialog, path, crop)
            return

        if self.crop_widget is not None:
            root = self._render_root_for_crop_widget(self.crop_widget)
            crop = _widget_bounds(
                self.crop_widget,
                root,
                padding=self.crop_padding,
                padding_top=self.crop_padding_top,
                padding_end=self.crop_padding_end,
                padding_bottom=self.crop_padding_bottom,
                padding_start=self.crop_padding_start,
            )
            if crop is None:
                raise RuntimeError("crop widget bounds are not available")
            self._render_widget_crop(root, path, crop)
            return

        if self.window is None:
            raise RuntimeError("cannot render screenshot without a main window")

        shot_crop = _shot_crop(shot)
        if shot_crop is not None:
            self._render_widget_crop(self.window, path, shot_crop)
            return
        try:
            _render_widget_png(self.window, path)
        except RuntimeError as exc:
            if "produced no render node" not in str(exc):
                raise
            window_id = _widget_window_id(self.window) or _active_window_id()
            _capture_root_window_area(
                path,
                window_id,
                (self.window.get_width(), self.window.get_height()),
            )

    def _render_root_for_crop_widget(self, widget: Gtk.Widget) -> Gtk.Widget:
        if isinstance(self.current_dialog, Gtk.Widget):
            if _raw_widget_bounds(widget, self.current_dialog) is not None:
                return self.current_dialog
        if self.window is not None and _raw_widget_bounds(widget, self.window) is not None:
            return self.window
        root = widget.get_root()
        if isinstance(root, Gtk.Widget):
            return root
        return widget

    def _render_widget_crop(
        self,
        widget: Gtk.Widget,
        path: Path,
        bounds: tuple[int, int, int, int],
    ) -> None:
        crop = _clamp_bounds_to_widget(widget, self._apply_crop_size(bounds))
        _render_widget_png(widget, path, _graphene_rect(crop))

    def _apply_crop_size(
        self,
        bounds: tuple[int, int, int, int],
    ) -> tuple[int, int, int, int]:
        x, y, width, height = bounds
        if self.crop_width is not None:
            width = self.crop_width
        if self.crop_height is not None:
            height = self.crop_height
        return x, y, width, height

    def _close_current_dialog(self) -> None:
        if self.current_popover is not None:
            self.current_popover.popdown()
            self.current_popover.unparent()
            self.current_popover = None
        _close_dialog(self.current_dialog)
        self.current_dialog = None
        _drain_events()

    def _close_welcome_placeholder(self) -> None:
        assert self.window is not None
        page = self.window._placeholder_page or tab_layout._page_for_child(
            self.window, self.window.placeholder
        )
        if page is not None:
            tab_layout._close_tab_page(self.window, page)
        self.window._placeholder_page = None

    def _isolate_welcome_tabs(self) -> None:
        assert self.window is not None
        for hardware_id, page in list(self.window._device_pages.items()):
            self.window._device_pages.pop(hardware_id, None)
            tab_layout._close_tab_page(self.window, page)
        if self.window._combo_page is not None:
            tab_layout._close_tab_page(self.window, self.window._combo_page)
        self.window.combo_tab = None
        self.window._combo_page = None
        device_tabs._ensure_placeholder_page(self.window)
        device_tabs._set_empty_placeholder_state(self.window)
        self._welcome_tabs_isolated = True
        _drain_events()

    def _restore_fixture_tabs_after_welcome(self) -> None:
        assert self.window is not None
        if not self._welcome_tabs_isolated:
            return
        self._close_welcome_placeholder()
        device_tabs._apply_loaded_devices(self.window, self.window.hardware_manager.list_hardware())
        device_tabs._setup_combo_tab(self.window)
        self._welcome_tabs_isolated = False
        _drain_events()

    def _resize_window(self, shot: Json) -> None:
        assert self.window is not None
        target = str(shot.get("target", "") or "")
        default_width = (
            self.selector_width
            if target in {"key_selector", "combo_action_selector"}
            else self.default_width
        )
        default_height = (
            self.selector_height
            if target in {"key_selector", "combo_action_selector"}
            else self.default_height
        )
        width = int(shot.get("window_width", default_width) or default_width)
        height = int(shot.get("window_height", default_height) or default_height)
        self.window.set_default_size(width, height)
        self.window.present()

    def _prepare_shot(self, shot: Json) -> None:
        target = str(shot.get("target", "") or "")
        dispatch = {
            "welcome": self._prepare_welcome,
            "add_device_dialog": self._prepare_add_device_dialog,
            "hardware_settings": self._prepare_hardware_settings,
            "device_fresh": self._prepare_device_fresh,
            "profile_device": self._prepare_profile_device,
            "device_button": self._prepare_device_button,
            "key_selector": self._prepare_key_selector,
            "macro_manager": self._prepare_macro_manager,
            "record_macro_dialog": self._prepare_record_macro_dialog,
            "save_macro_dialog": self._prepare_save_macro_dialog,
            "macro_editor": self._prepare_macro_editor,
            "macro_editor_loop_menu": self._prepare_macro_editor,
            "macro_editor_timing_tools": self._prepare_macro_editor,
            "type_macro_dialog": self._prepare_type_macro_dialog,
            "superkey_dialog": self._prepare_superkey_dialog,
            "analog_controls_manager": self._prepare_analog_controls_manager,
            "combos_tab": self._prepare_combos_tab,
            "combo_editor": self._prepare_combo_editor,
            "combo_action_selector": self._prepare_combo_action_selector,
            "gnome_setup_dialog": self._prepare_gnome_setup_dialog,
        }
        handler = dispatch.get(target)
        if handler is None:
            raise ValueError(f"unknown screenshot target {target!r}")
        handler(shot)

    def _select_device_profile(self, shot: Json, *, default_profile: str = "Desktop") -> Gtk.Widget:
        assert self.window is not None
        hardware_id = str(shot.get("device", "35ef:0021") or "35ef:0021")
        profile = str(shot.get("profile", default_profile) or default_profile)
        page = self.window._device_pages.get(hardware_id)
        if page is None:
            raise KeyError(f"device tab {hardware_id!r} is not loaded")
        profiles._sync_selected_profile_name(self.window, profile)
        tab = page.get_child()
        if hasattr(tab, "refresh_profiles"):
            tab.refresh_profiles(preferred_profile_name=profile, publish_selection=False)
        self.window.tab_view.set_selected_page(page)
        self.window.present()
        return tab

    def _prepare_welcome(self, shot: Json) -> None:
        assert self.window is not None
        self._isolate_welcome_tabs()
        placeholder = self.window.placeholder
        device_tabs._ensure_placeholder_page(self.window)
        device_tabs._set_empty_placeholder_state(self.window)
        page = self.window._placeholder_page or tab_layout._page_for_child(self.window, placeholder)
        if page is None:
            raise RuntimeError("welcome placeholder page is not available")
        self.window._placeholder_page = page
        self.window.tab_view.set_selected_page(page)
        self.window.present()

    def _prepare_add_device_dialog(self, shot: Json) -> None:
        assert self.window is not None
        dialog = HardwareSetupDialog(self.window, self.window.hardware_manager)
        dialog.present(self.window)
        self.current_dialog = dialog
        self._set_dialog_crop(dialog, shot)
        device_list = getattr(dialog, "device_list", None)
        if isinstance(device_list, Gtk.Widget):
            _stabilize_ancestor_scrollbar(device_list)

    def _prepare_hardware_settings(self, shot: Json) -> None:
        assert self.window is not None
        hardware_id = str(shot.get("device", "1532:00b4") or "1532:00b4")
        hardware = self.window.hardware_manager.get_hardware(hardware_id)
        if hardware is None:
            raise KeyError(f"hardware {hardware_id!r} is not loaded")

        config = deepcopy(hardware)
        self._prepare_hardware_settings_fixture(config, shot)

        dialog = HardwareSettingsDialog(
            self.window,
            config,
            self.window.hardware_manager,
            self._docshot_add_evdev_devices,
            self._docshot_delete_hardware,
            self._docshot_delete_evdev_device,
            self._docshot_set_detection_method,
            self._docshot_stable_detection_status,
            self._docshot_rename_hardware,
            can_delete_profile_mappings=True,
        )
        dialog.present(self.window)
        self.current_dialog = dialog
        self._set_dialog_crop(dialog, shot)

    def _prepare_hardware_settings_fixture(
        self,
        config: HardwareConfig,
        shot: Json,
    ) -> None:
        variant = str(shot.get("hardware_variant", "stable") or "stable")
        if config.hardware_id == "1532:00b4":
            for device in config.evdev_devices:
                if device.id == "mouse":
                    device.path = (
                        "/dev/input/by-id/"
                        "usb-Razer_Razer_Naga_V2_HyperSpeed_000000000000-event-mouse"
                    )
                elif device.id == "if02":
                    device.path = (
                        "/dev/input/by-id/"
                        "usb-Razer_Razer_Naga_V2_HyperSpeed_000000000000-if02-event-kbd"
                    )

        if variant != "product_id":
            return

        product_path = make_keymasq_device_path(config.vendor_id, config.product_id)
        for device in config.evdev_devices:
            device.path = product_path
            if not device.capabilities:
                device.capabilities = [f"type:{device.device_type.value}", str(device.id or "")]

    def _docshot_add_evdev_devices(self, _devices: list[EvdevDevice]) -> int:
        return 0

    def _docshot_delete_hardware(self) -> None:
        return

    def _docshot_delete_evdev_device(
        self,
        _device: EvdevDevice,
        _delete_profile_mappings: bool,
    ) -> bool:
        return False

    def _docshot_set_detection_method(
        self,
        _device: EvdevDevice,
        method: DetectionMethod,
    ) -> tuple[bool, str]:
        label = "Stable Path" if method == "stable" else "Product ID"
        return True, f"Switched event device to {label} detection."

    def _docshot_stable_detection_status(self, device: EvdevDevice) -> tuple[bool, str]:
        path = str(device.path or "")
        if is_by_id_path(path):
            return True, "Match this event device by its /dev/input/by-id path."
        if is_keymasq_device_path(path):
            return (
                False,
                "Stable Path is unavailable because this event device has no "
                "/dev/input/by-id path.",
            )
        return (
            False,
            "Stable Path is unavailable because this event device has no /dev/input/by-id path.",
        )

    def _docshot_rename_hardware(self, refresh: Callable[[], None]) -> None:
        refresh()

    def _prepare_device_fresh(self, shot: Json) -> None:
        self._select_device_profile(
            {"device": str(shot.get("device", "35ef:0021")), "profile": "Default"},
            default_profile="Default",
        )

    def _prepare_profile_device(self, shot: Json) -> None:
        self._select_device_profile(shot)

    def _prepare_device_button(self, shot: Json) -> None:
        tab = self._select_device_profile(shot)
        source = str(shot.get("source", "") or "")
        button_widgets = getattr(tab, "_button_widgets", {})
        widget = button_widgets.get(source)
        if widget is None:
            raise KeyError(f"button widget {source!r} was not found")
        self._set_widget_crop(widget, shot)

    def _current_action(self, shot: Json) -> MappingAction | None:
        assert self.window is not None
        profile_name = str(shot.get("profile", "Desktop") or "Desktop")
        hardware_id = str(shot.get("device", "35ef:0021") or "35ef:0021")
        source = str(shot.get("source", "") or "")
        profile = self.window.profile_manager.get_profile(profile_name)
        if profile is None:
            return None
        layer = profile.config.get_layer(hardware_id)
        if layer is None:
            return None
        return layer.mappings.get(source)

    def _button_label(self, device: HardwareConfig, source: str) -> tuple[str, str, str | None]:
        for button in device.buttons:
            if button.id == source:
                return button.label or button.id, "button", None
        for analog in device.analog_inputs:
            if analog.id == source:
                return analog.label or analog.id, "analog", analog.type
        raise KeyError(f"source {source!r} not found on device {device.name!r}")

    def _compositor_status(self, shot: Json) -> Json:
        compositor = str(shot.get("compositor", "hyprland") or "hyprland")
        status = dict(DEFAULT_COMPOSITOR_STATUS)
        status["compositor_id"] = compositor
        status["listener_name"] = compositor
        status["compositor_dispatch_available"] = True
        return status

    def _prepare_key_selector(self, shot: Json) -> None:
        assert self.window is not None
        tab = self._select_device_profile(shot)
        device = getattr(tab, "device", None)
        if device is None:
            raise RuntimeError("selected tab has no device")
        source = str(shot.get("source", "") or "")
        label, source_type, analog_input_type = self._button_label(device, source)
        dialog = KeySelectorDialog(
            self.window,
            label,
            self._current_action(shot),
            compositor_action_status=self._compositor_status(shot),
            source_type=source_type,
            analog_input_type=analog_input_type,
        )
        _set_dialog_stack(dialog, shot.get("selector_tab"))
        dialog.present(self.window)
        self.current_dialog = dialog
        self._set_dialog_crop(dialog, shot)

    def _prepare_macro_manager(self, shot: Json) -> None:
        assert self.window is not None
        dialog = MacroManagerDialog(self.window)
        dialog.present(self.window)
        self.current_dialog = dialog
        self._set_dialog_crop(dialog, shot)

    def _prepare_record_macro_dialog(self, shot: Json) -> None:
        assert self.window is not None
        dialog = RecordMacroDialog(self.window)
        _install_record_macro_dialog_fixture(dialog)
        dialog.present(self.window)
        self.current_dialog = dialog
        self._set_dialog_crop(dialog, shot)

    def _prepare_save_macro_dialog(self, shot: Json) -> None:
        assert self.window is not None
        dialog = SaveMacroDialog(
            self.window,
            {
                "recording_slot": 1,
                "pending_save_token": "docshot",
                "start_position_recorded": True,
                "block_mouse_movement": True,
            },
        )
        dialog.present(self.window)
        self.current_dialog = dialog
        self._set_dialog_crop(dialog, shot)

    def _prepare_macro_editor(self, shot: Json) -> None:
        assert self.window is not None
        target = str(shot.get("target", "") or "")
        macro = str(shot.get("macro", "volume_up") or "volume_up")
        dialog = MacroEditorDialog(self.window, macro, select_initial_event=False)
        dialog.present(self.window)
        self.current_dialog = dialog
        if target == "macro_editor_timing_tools":
            self.capture_root_window = True
            GLib.timeout_add(300, self._show_macro_timing_tools_popover, dialog)
            GLib.timeout_add(600, self._show_macro_timing_tools_popover, dialog)
            return
        if target == "macro_editor_loop_menu":
            self.capture_root_window = True
            self._set_dialog_crop(dialog, shot)
            GLib.timeout_add(300, self._open_macro_loop_dropdown, dialog)
            GLib.timeout_add(600, self._open_macro_loop_dropdown, dialog)
            return
        self._set_dialog_crop(dialog, shot)

    def _show_macro_timing_tools_popover(self, dialog: MacroEditorDialog) -> bool:
        if self.current_popover is not None:
            self.current_popover.popdown()
            self.current_popover.unparent()
            self.current_popover = None
        build_popover = getattr(dialog, "_build_timing_popover", None)
        if not callable(build_popover):
            return False
        popover = build_popover()
        parent = _find_menu_button_by_label(dialog, "Timing Tools") or dialog
        popover.set_parent(parent)
        popover.popup()
        self.current_popover = popover
        return False

    def _open_macro_loop_dropdown(self, dialog: MacroEditorDialog) -> bool:
        dropdown = getattr(dialog, "_macro_loop_mode_combo", None)
        if not isinstance(dropdown, Gtk.Widget):
            return False
        dropdown.grab_focus()
        activate = getattr(dropdown, "activate", None)
        if callable(activate) and activate():
            return False
        self._click_widget_center(dropdown)
        return False

    def _click_widget_center(self, widget: Gtk.Widget) -> None:
        assert self.window is not None
        bounds = _raw_widget_bounds(widget, self.window)
        window_id = _widget_window_id(self.window)
        if bounds is None or window_id is None:
            return
        geometry = _window_geometry(window_id)
        if geometry is None:
            return
        root_x, root_y, _, _ = geometry
        x, y, width, height = bounds
        subprocess.run(
            [
                "xdotool",
                "mousemove",
                str(root_x + x + width // 2),
                str(root_y + y + height // 2),
                "click",
                "1",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def _prepare_type_macro_dialog(self, shot: Json) -> None:
        assert self.window is not None
        dialog = TypeMacroDialog(self.window)
        dialog.name_entry.set_text(str(shot.get("macro_name", "type_docs_search")))
        dialog.present(self.window)
        self.current_dialog = dialog
        self._set_dialog_crop(dialog, shot)

    def _prepare_superkey_dialog(self, shot: Json) -> None:
        assert self.window is not None
        superkey = str(shot.get("superkey", "") or "")
        dialog = SuperkeyDialog(self.window, self.window.profile_manager)
        if superkey:
            row = _find_superkey_dialog_row(dialog, superkey)
            if row is not None:
                dialog.shell.list_box.select_row(row)
        dialog.present(self.window)
        self.current_dialog = dialog
        self._set_dialog_crop(dialog, shot)
        self._apply_declared_crop_widget(dialog.editor, shot)

    def _prepare_analog_controls_manager(self, shot: Json) -> None:
        assert self.window is not None
        dialog = AnalogControlDialog(self.window, self.window.profile_manager)
        _apply_dialog_size(dialog, shot)
        analog_control = str(shot.get("analog_control", "") or "").strip()
        if analog_control:
            row = _find_analog_control_dialog_row(dialog, analog_control)
            if row is None:
                raise KeyError(f"analog control {analog_control!r} was not found")
            dialog.shell.list_box.select_row(row)
        expand_threshold = _optional_int(shot.get("expand_threshold"))
        if expand_threshold is not None:
            rows = dialog.editor.thresholds.rows
            if 0 <= expand_threshold < len(rows):
                rows[expand_threshold].row.set_expanded(True)
        expand_row_title = str(shot.get("expand_row_title", "") or "").strip()
        if expand_row_title:
            _expand_expander_row_by_title(dialog, expand_row_title)
        dialog.present(self.window)
        self.current_dialog = dialog
        self._set_dialog_crop(dialog, shot)

    def _prepare_combos_tab(self, shot: Json) -> None:
        assert self.window is not None
        profiles._sync_selected_profile_name(
            self.window, str(shot.get("profile", "Desktop") or "Desktop")
        )
        self.window.show_combo_tab()

    def _combo_profile(self, shot: Json) -> ProfileInfo | None:
        assert self.window is not None
        return self.window.profile_manager.get_profile(
            str(shot.get("profile", "Desktop") or "Desktop")
        )

    def _set_widget_crop(
        self,
        widget: Gtk.Widget,
        shot: Json,
        *,
        default_padding: int = 0,
        default_padding_top: int | None = None,
        default_padding_end: int | None = None,
        default_padding_bottom: int | None = None,
        default_padding_start: int | None = None,
    ) -> None:
        crop_padding = _optional_int(shot.get("crop_padding"))
        self.crop_widget = widget
        self.crop_dialog = None
        self.crop_padding = crop_padding if crop_padding is not None else default_padding
        self.crop_padding_top = _optional_int(shot.get("crop_padding_top"))
        self.crop_padding_end = _optional_int(shot.get("crop_padding_end"))
        self.crop_padding_bottom = _optional_int(shot.get("crop_padding_bottom"))
        self.crop_padding_start = _optional_int(shot.get("crop_padding_start"))
        if self.crop_padding_top is None:
            self.crop_padding_top = default_padding_top
        if self.crop_padding_end is None:
            self.crop_padding_end = default_padding_end
        if self.crop_padding_bottom is None:
            self.crop_padding_bottom = default_padding_bottom
        if self.crop_padding_start is None:
            self.crop_padding_start = default_padding_start
        self.crop_width = _optional_int(shot.get("crop_width"))
        self.crop_height = _optional_int(shot.get("crop_height"))

    def _apply_declared_crop_widget(self, owner: object, shot: Json) -> None:
        raw_name = str(shot.get("crop_widget", "") or "").strip()
        if not raw_name:
            return
        if raw_name in {"self", "dialog"}:
            widget = owner if isinstance(owner, Gtk.Widget) else None
        else:
            widget = getattr(owner, raw_name, None)
        if not isinstance(widget, Gtk.Widget):
            raise ValueError(f"crop widget {raw_name!r} was not found")
        self._set_widget_crop(widget, shot)
        if bool(shot.get("scroll_crop_widget", False)):
            GLib.timeout_add(100, _scroll_widget_to_bottom, widget)
            GLib.timeout_add(300, _scroll_widget_to_bottom, widget)

    def _set_dialog_crop(
        self,
        dialog: object,
        shot: Json,
        *,
        default_padding: int = 0,
        default_padding_top: int | None = None,
        default_padding_end: int | None = None,
        default_padding_bottom: int | None = None,
        default_padding_start: int | None = None,
    ) -> None:
        if not isinstance(dialog, Gtk.Widget):
            return
        _apply_dialog_size(dialog, shot)
        self._set_widget_crop(
            dialog,
            shot,
            default_padding=default_padding,
            default_padding_top=default_padding_top,
            default_padding_end=default_padding_end,
            default_padding_bottom=default_padding_bottom,
            default_padding_start=default_padding_start,
        )
        self.crop_dialog = dialog

    def _prepare_combo_editor(self, shot: Json) -> None:
        assert self.window is not None
        profile = self._combo_profile(shot)
        dialog = ComboEditorDialog(
            self.window,
            profile_name=profile.config.name if profile else "Desktop",
            sibling_combos=list(profile.config.combos) if profile else [],
        )
        dialog.present(self.window)
        self.current_dialog = dialog
        self._set_dialog_crop(dialog, shot)

    def _prepare_combo_action_selector(self, shot: Json) -> None:
        assert self.window is not None
        action = MappingAction(
            action_type=ActionType.COMPOSITOR_DISPATCH,
            compositor_id=str(shot.get("compositor", "hyprland") or "hyprland"),
            compositor_dispatcher='hl.dsp.focus({ workspace = "1" })',
            compositor_args="",
        )
        dialog = KeySelectorDialog(
            self.window,
            "Combo Action",
            action,
            compositor_action_status=self._compositor_status(shot),
            allow_passthrough=False,
            allow_clear_mapping=False,
        )
        _set_dialog_stack(dialog, shot.get("selector_tab"))
        dialog.present(self.window)
        self.current_dialog = dialog
        self._set_dialog_crop(dialog, shot)

    def _prepare_gnome_setup_dialog(self, shot: Json) -> None:
        assert self.window is not None
        state = str(shot.get("gnome_state", "shell_not_rescanned") or "shell_not_rescanned")
        if state == "bridge_disabled":
            details: Json = {
                "gnome_bridge_state": "bridge_disabled",
                "gnome_bridge_action": "enable_bridge",
            }
        else:
            details = {
                "gnome_bridge_state": "shell_not_rescanned",
                "gnome_bridge_action": "logout",
            }
        dialog = GnomeSetupDialog(self.window, details)
        dialog.present(self.window)
        self.current_dialog = dialog
        self._set_dialog_crop(
            dialog,
            shot,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--modes", default="dark,light")
    args = parser.parse_args()

    os.environ.setdefault("GDK_BACKEND", "x11")
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    if not modes:
        raise ValueError("at least one screenshot mode is required")

    # Fail early with a clearer message than a GTK backtrace if the session is absent.
    status = session_request({"command": "get_status"}, timeout=2.0)
    if not isinstance(status, dict):
        raise RuntimeError("keymasq-session is not responding")

    app = Application(demo_mode=False)
    _install_docshot_css()
    runner = DocshotRunner(
        app=app,
        manifest=args.manifest,
        output_root=args.output_root,
        modes=modes,
    )

    def on_activate(_app: Application) -> None:
        GLib.timeout_add(100, runner.start)

    app.connect("activate", on_activate)
    status = int(app.run(["keymasq-docshot-driver"]))
    if status:
        return status
    return 1 if runner.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
