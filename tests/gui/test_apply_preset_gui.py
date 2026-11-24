# ruff: noqa: E402
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import AnalogControlConfig, AnalogMouseMotionConfig
from keymasq.common.model.core import ActionType, DeviceType
from keymasq.common.model.hardware import (
    AnalogInputDefinition,
    ButtonDefinition,
    EvdevDevice,
    HardwareConfig,
)
from keymasq.common.model.profiles import ProfileConfig
from keymasq.gui.session_client import GuiTaskResult
from keymasq.gui.widgets.combo_tab import ComboTab
from keymasq.gui.widgets.device_tab import preset_dialog as preset_dialog_module
from keymasq.gui.widgets.device_tab.tab import DeviceTab
from keymasq.session.analog_controls import AnalogControlManager
from keymasq.session.presets import load_presets
from keymasq.session.profile.manager import ProfileManager
from tests.gui.support import collect_widgets

DESKTOP_PRESET_TOML = """
[preset]
name = "Desktop & Mouse"
description = "Control pointer <fast> & keys"
device_types = ["gamepad"]

[mapping.btn_south]
action = "keyboard"
target = "key_enter"

[mapping.btn_east]
action = "keyboard"
target = "key_esc"

[mapping.btn_mode]
action = "keyboard"
target = "key_leftmeta"

[mapping.right_stick]
action = "analog_control"
analog_control_name = "Pointer"

[mapping.left_trigger]
action = "analog_control"
analog_control_name = "Pointer"

[analog_controls.Pointer]
input_type = "stick"

[analog_controls.Pointer.mouse_motion]
enabled = true
speed = 700.0
"""

MODEL_PRESET_TOML = """
[preset]
name = "Deck Custom"
description = "Model-specific layout"
hardware = ["28de:1205"]

[mapping.btn_south]
action = "keyboard"
target = "key_space"
"""


def _gamepad(vendor_id: str = "045e", product_id: str = "028e") -> HardwareConfig:
    return HardwareConfig(
        vendor_id=vendor_id,
        product_id=product_id,
        name="Test & Pad",
        evdev_devices=[EvdevDevice("/dev/input/event0", DeviceType.GAMEPAD, "pad")],
        buttons=[
            ButtonDefinition(id="btn_south", label="A Button", evdev="btn_south", source="pad"),
            ButtonDefinition(id="btn_east", label="B Button", evdev="btn_east", source="pad"),
        ],
        analog_inputs=[
            AnalogInputDefinition(id="right_stick", label="Right Stick", type="stick"),
            AnalogInputDefinition(id="left_trigger", label="Left Trigger", type="axis"),
        ],
    )


def _keyboard() -> HardwareConfig:
    return HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Plain Keyboard",
        evdev_devices=[EvdevDevice("/dev/input/event1", DeviceType.KEYBOARD, "kb")],
        buttons=[ButtonDefinition(id="key_a", label="A", evdev="key_a", source="kb")],
    )


def _write_preset(directory: Path, stem: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}.toml").write_text(body, encoding="utf-8")


def _root_content(widget: Gtk.Widget) -> Gtk.Widget:
    if isinstance(widget, (Adw.Dialog, Adw.NavigationPage)):
        child = widget.get_child()
        if child is not None:
            return child
    return widget


def _find_button_by_tooltip(root: Gtk.Widget, tooltip: str) -> Gtk.Button | None:
    for button in collect_widgets(_root_content(root), Gtk.Button):
        if button.get_tooltip_text() == tooltip:
            return button
    return None


def _find_button_by_label(root: Gtk.Widget, label: str) -> Gtk.Button | None:
    for button in collect_widgets(_root_content(root), Gtk.Button):
        if button.get_label() == label:
            return button
    return None


def _all_text(widget: Gtk.Widget) -> list[str]:
    root = _root_content(widget)
    texts: list[str] = []
    for label in collect_widgets(root, Gtk.Label):
        text = label.get_text()
        if text:
            texts.append(text)
    for page in collect_widgets(root, Adw.StatusPage):
        if page.get_title():
            texts.append(page.get_title())
        if page.get_description():
            texts.append(page.get_description())
    for row in collect_widgets(root, Adw.ActionRow):
        if row.get_title():
            texts.append(row.get_title())
        if row.get_subtitle():
            texts.append(row.get_subtitle())
    return texts


def _action_rows(widget: Gtk.Widget) -> list[Adw.ActionRow]:
    return collect_widgets(_root_content(widget), Adw.ActionRow)


class _ToastWindow:
    def __init__(self) -> None:
        self._selected_profile_name: str | None = None
        self._syncing_profile_selection = True
        self.toasts: list[str] = []

    def show_toast(self, title: str) -> None:
        self.toasts.append(title)


def _install_sync_gui_task(monkeypatch):
    calls: list[object] = []

    def run_sync(worker, callback, *, on_start=None, on_done=None):
        calls.append(worker)
        if on_start is not None:
            on_start()
        try:
            result = GuiTaskResult(value=worker())
        except (OSError, ValueError, RuntimeError) as exc:
            result = GuiTaskResult(error=exc)
        callback(result)
        if on_done is not None:
            on_done()

    monkeypatch.setattr(preset_dialog_module, "run_gui_task", run_sync)
    return calls


def _install_deferred_gui_task(monkeypatch):
    pending: list[
        tuple[Callable[[], Any], Callable[[GuiTaskResult[Any]], object], Callable[[], None] | None]
    ] = []

    def defer(worker, callback, *, on_start=None, on_done=None):
        if on_start is not None:
            on_start()
        pending.append((worker, callback, on_done))

    def flush_next() -> None:
        worker, callback, on_done = pending.pop(0)
        try:
            result = GuiTaskResult(value=worker())
        except (OSError, ValueError, RuntimeError) as exc:
            result = GuiTaskResult(error=exc)
        callback(result)
        if on_done is not None:
            on_done()

    monkeypatch.setattr(preset_dialog_module, "run_gui_task", defer)
    return pending, flush_next


def _capture_presented_dialogs(monkeypatch):
    presented: list[Adw.Dialog] = []

    def fake_present(self, _parent=None):
        presented.append(self)

    monkeypatch.setattr(preset_dialog_module.ApplyPresetDialog, "present", fake_present)
    return presented


def test_apply_preset_button_state_on_device_and_combo_tabs(temp_config_dir):
    profile_manager = ProfileManager()
    profile_manager.save_profile(ProfileConfig(name="Gaming"))

    tab = DeviceTab(device=_gamepad(), profile_manager=profile_manager)
    button = _find_button_by_tooltip(tab, "Apply preset")
    assert button is not None
    assert button.get_sensitive() is True

    tab.profile_dropdown.set_selected(0)
    assert button.get_sensitive() is False

    tab.profile_dropdown.set_selected(1)
    assert button.get_sensitive() is True

    tab.device = None
    tab._update_apply_preset_button_state()
    assert button.get_sensitive() is False

    demo_tab = DeviceTab(device=_gamepad(), profile_manager=profile_manager, demo_mode=True)
    demo_button = _find_button_by_tooltip(demo_tab, "Apply preset")
    assert demo_button is not None
    assert demo_button.get_sensitive() is False

    no_manager_tab = DeviceTab(device=_gamepad(), profile_manager=None)
    no_manager_button = _find_button_by_tooltip(no_manager_tab, "Apply preset")
    assert no_manager_button is not None
    assert no_manager_button.get_sensitive() is False

    combo_tab = ComboTab(profile_manager=profile_manager)
    assert _find_button_by_tooltip(combo_tab, "Apply preset") is None


def test_dialog_loads_presets_async_and_orders_model_specific_first(
    temp_config_dir, tmp_path, monkeypatch
):
    preset_dir = tmp_path / "presets"
    _write_preset(preset_dir, "desktop", DESKTOP_PRESET_TOML)
    _write_preset(preset_dir, "deck", MODEL_PRESET_TOML)
    monkeypatch.setattr(preset_dialog_module, "load_presets", lambda: load_presets([preset_dir]))
    pending, flush_next = _install_deferred_gui_task(monkeypatch)
    presented = _capture_presented_dialogs(monkeypatch)

    profile_manager = ProfileManager()
    profile_manager.save_profile(ProfileConfig(name="Desk"))
    tab = DeviceTab(device=_gamepad("28de", "1205"), profile_manager=profile_manager)

    button = _find_button_by_tooltip(tab, "Apply preset")
    assert button is not None
    button.emit("clicked")

    assert len(presented) == 1
    assert len(pending) == 1
    dialog = presented[0]
    assert dialog._list_stack.get_visible_child_name() == "loading"

    flush_next()

    assert dialog._list_stack.get_visible_child_name() == "list"
    rows = _action_rows(dialog)
    assert [(row.get_title(), row.get_subtitle()) for row in rows] == [
        ("Deck Custom", "Model-specific layout"),
        ("Desktop & Mouse", "Control pointer <fast> & keys"),
    ]


def test_dialog_shows_empty_state_with_user_presets_path(temp_config_dir, tmp_path, monkeypatch):
    preset_dir = tmp_path / "presets"
    _write_preset(preset_dir, "desktop", DESKTOP_PRESET_TOML)
    monkeypatch.setattr(preset_dialog_module, "load_presets", lambda: load_presets([preset_dir]))
    _install_sync_gui_task(monkeypatch)
    presented = _capture_presented_dialogs(monkeypatch)

    profile_manager = ProfileManager()
    profile_manager.save_profile(ProfileConfig(name="Desk"))
    tab = DeviceTab(device=_keyboard(), profile_manager=profile_manager)

    button = _find_button_by_tooltip(tab, "Apply preset")
    assert button is not None
    button.emit("clicked")

    dialog = presented[0]
    assert dialog._list_stack.get_visible_child_name() == "empty"
    combined = "\n".join(_all_text(dialog))
    assert "~/.config/keymasq/presets" in combined


def test_preview_shows_fill_kept_unavailable_and_writes_only_on_confirm(
    temp_config_dir, tmp_path, monkeypatch
):
    preset_dir = tmp_path / "presets"
    _write_preset(preset_dir, "desktop", DESKTOP_PRESET_TOML)
    monkeypatch.setattr(preset_dialog_module, "load_presets", lambda: load_presets([preset_dir]))
    pending, flush_next = _install_deferred_gui_task(monkeypatch)
    presented = _capture_presented_dialogs(monkeypatch)

    device = _gamepad()
    profile = ProfileConfig(name="Desk")
    layer = profile.ensure_layer(device.hardware_id)
    layer.mappings["btn_south"] = MappingAction(ActionType.KEYBOARD, target="key_a")
    layer.mappings["btn_east"] = MappingAction(ActionType.PASSTHROUGH)
    profile_manager = ProfileManager()
    profile_manager.save_profile(profile)

    AnalogControlManager().save_analog_control(
        AnalogControlConfig(
            name="Pointer",
            input_type="stick",
            mouse_motion=AnalogMouseMotionConfig(enabled=True, speed=250.0),
        )
    )

    reloads: list[bool] = []
    window = _ToastWindow()
    tab = DeviceTab(device=device, profile_manager=profile_manager, main_window=window)
    monkeypatch.setattr(tab, "_notify_session_reload_async", lambda: reloads.append(True))

    button = _find_button_by_tooltip(tab, "Apply preset")
    assert button is not None
    button.emit("clicked")
    flush_next()

    dialog = presented[0]
    preset_row = _action_rows(dialog)[0]
    preset_row.emit("activated")

    preview_page = dialog._nav_view.get_visible_page()
    assert preview_page is not None
    assert preview_page.get_tag() == "preview"
    preview_text = "\n".join(_all_text(preview_page))
    assert "2 controls will be filled" in preview_text
    assert "B Button" in preview_text
    assert "Right Stick" in preview_text
    assert "A Button" in preview_text
    assert "Not present on this device\nGuide" in preview_text
    assert "Incompatible action for this control\nLeft Trigger" in preview_text

    saved_before = ProfileManager().get_profile("Desk")
    assert saved_before is not None
    assert "right_stick" not in saved_before.config.device_layers[device.hardware_id].mappings

    apply_btn = _find_button_by_label(preview_page, "Apply Preset")
    assert apply_btn is not None
    apply_btn.emit("clicked")

    assert len(pending) == 1
    assert apply_btn.get_sensitive() is False
    assert dialog.get_can_close() is False

    flush_next()

    saved_after = ProfileManager().get_profile("Desk")
    assert saved_after is not None
    mappings = saved_after.config.device_layers[device.hardware_id].mappings
    assert mappings["btn_south"].target == "key_a"
    assert mappings["btn_east"].action_type == ActionType.KEYBOARD
    assert mappings["btn_east"].target == "key_esc"
    assert mappings["right_stick"].action_type == ActionType.ANALOG_CONTROL
    assert mappings["right_stick"].analog_control_name == "Pointer"

    reused = AnalogControlManager().get_analog_control("Pointer")
    assert reused is not None
    assert reused.mouse_motion.speed == 250.0

    assert "key_esc" in tab._button_widgets["btn_east"]._action_label.get_text()
    assert "Pointer" in tab._button_widgets["right_stick"]._action_label.get_text()
    assert "3 mappings" in tab._header_caption_label.get_text()
    assert reloads == [True]
    assert window.toasts == ["Applied 'Desktop & Mouse': 2 filled, 1 kept"]


def test_preview_omits_apply_button_when_nothing_would_be_filled(
    temp_config_dir, tmp_path, monkeypatch
):
    preset_dir = tmp_path / "presets"
    _write_preset(preset_dir, "deck", MODEL_PRESET_TOML)
    monkeypatch.setattr(preset_dialog_module, "load_presets", lambda: load_presets([preset_dir]))
    _install_sync_gui_task(monkeypatch)
    presented = _capture_presented_dialogs(monkeypatch)

    device = _gamepad("28de", "1205")
    profile = ProfileConfig(name="Desk")
    profile.ensure_layer(device.hardware_id).mappings["btn_south"] = MappingAction(
        ActionType.KEYBOARD, target="key_z"
    )
    profile_manager = ProfileManager()
    profile_manager.save_profile(profile)

    tab = DeviceTab(device=device, profile_manager=profile_manager)
    button = _find_button_by_tooltip(tab, "Apply preset")
    assert button is not None
    button.emit("clicked")

    dialog = presented[0]
    _action_rows(dialog)[0].emit("activated")

    preview_page = dialog._nav_view.get_visible_page()
    assert preview_page is not None
    assert "Nothing to fill" in "\n".join(_all_text(preview_page))
    assert _find_button_by_label(preview_page, "Apply Preset") is None
    assert _find_button_by_label(preview_page, "Close") is not None


def test_apply_saves_embedded_analog_controls(temp_config_dir, tmp_path, monkeypatch):
    preset_dir = tmp_path / "presets"
    _write_preset(preset_dir, "desktop", DESKTOP_PRESET_TOML)
    monkeypatch.setattr(preset_dialog_module, "load_presets", lambda: load_presets([preset_dir]))
    _install_sync_gui_task(monkeypatch)
    presented = _capture_presented_dialogs(monkeypatch)

    device = _gamepad()
    profile_manager = ProfileManager()
    profile_manager.save_profile(ProfileConfig(name="Desk"))

    window = _ToastWindow()
    tab = DeviceTab(device=device, profile_manager=profile_manager, main_window=window)
    monkeypatch.setattr(tab, "_notify_session_reload_async", lambda: None)

    button = _find_button_by_tooltip(tab, "Apply preset")
    assert button is not None
    button.emit("clicked")

    dialog = presented[0]
    _action_rows(dialog)[0].emit("activated")
    preview_page = dialog._nav_view.get_visible_page()
    assert preview_page is not None
    assert "Creates analog controls: Pointer" in "\n".join(_all_text(preview_page))

    apply_btn = _find_button_by_label(preview_page, "Apply Preset")
    assert apply_btn is not None
    apply_btn.emit("clicked")

    created = AnalogControlManager().get_analog_control("Pointer")
    assert created is not None
    assert created.mouse_motion.speed == 700.0
    assert window.toasts == ["Applied 'Desktop & Mouse': 3 mappings filled"]


def test_closing_the_dialog_while_applying_writes_no_mappings(
    temp_config_dir, tmp_path, monkeypatch
):
    preset_dir = tmp_path / "presets"
    _write_preset(preset_dir, "desktop", DESKTOP_PRESET_TOML)
    monkeypatch.setattr(preset_dialog_module, "load_presets", lambda: load_presets([preset_dir]))
    _pending, flush_next = _install_deferred_gui_task(monkeypatch)
    presented = _capture_presented_dialogs(monkeypatch)

    device = _gamepad()
    profile_manager = ProfileManager()
    profile_manager.save_profile(ProfileConfig(name="Desk"))
    window = _ToastWindow()
    tab = DeviceTab(device=device, profile_manager=profile_manager, main_window=window)
    monkeypatch.setattr(tab, "_notify_session_reload_async", lambda: None)

    button = _find_button_by_tooltip(tab, "Apply preset")
    assert button is not None
    button.emit("clicked")
    dialog = presented[0]
    flush_next()
    _action_rows(dialog)[0].emit("activated")
    apply_btn = _find_button_by_label(dialog._nav_view.get_visible_page(), "Apply Preset")
    assert apply_btn is not None
    apply_btn.emit("clicked")

    dialog.emit("closed")
    flush_next()

    saved = ProfileManager().get_profile("Desk")
    assert saved is not None
    assert saved.config.get_layer(device.hardware_id) is None
    assert window.toasts == []


def test_dialog_reports_load_and_apply_errors(temp_config_dir, tmp_path, monkeypatch):
    def fail_load():
        raise OSError("disk unavailable")

    monkeypatch.setattr(preset_dialog_module, "load_presets", fail_load)
    _install_sync_gui_task(monkeypatch)
    presented = _capture_presented_dialogs(monkeypatch)

    profile_manager = ProfileManager()
    profile_manager.save_profile(ProfileConfig(name="Desk"))
    tab = DeviceTab(device=_gamepad(), profile_manager=profile_manager)

    button = _find_button_by_tooltip(tab, "Apply preset")
    assert button is not None
    button.emit("clicked")

    error_dialog = presented[0]
    assert error_dialog._list_stack.get_visible_child_name() == "error"
    assert "disk unavailable" in "\n".join(_all_text(error_dialog))
    tab._apply_preset_dialog = None

    preset_dir = tmp_path / "presets"
    _write_preset(preset_dir, "deck", MODEL_PRESET_TOML)
    monkeypatch.setattr(preset_dialog_module, "load_presets", lambda: load_presets([preset_dir]))
    monkeypatch.setattr(
        profile_manager,
        "save_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read-only filesystem")),
    )

    tab2 = DeviceTab(device=_gamepad("28de", "1205"), profile_manager=profile_manager)
    btn2 = _find_button_by_tooltip(tab2, "Apply preset")
    assert btn2 is not None
    btn2.emit("clicked")

    apply_dialog = presented[1]
    _action_rows(apply_dialog)[0].emit("activated")
    preview_page = apply_dialog._nav_view.get_visible_page()
    assert preview_page is not None
    apply_btn = _find_button_by_label(preview_page, "Apply Preset")
    assert apply_btn is not None
    apply_btn.emit("clicked")

    assert apply_dialog.get_can_close() is True
    assert apply_btn.get_sensitive() is True
    assert "read-only filesystem" in "\n".join(_all_text(preview_page))


def test_main_window_show_toast_adds_toast_to_overlay(temp_config_dir, monkeypatch):
    from keymasq.gui.window import _runtime as window_runtime
    from keymasq.gui.window.core import MainWindow

    monkeypatch.setattr(window_runtime, "run_gui_task", lambda _worker, _cb: None)
    monkeypatch.setattr(window_runtime, "session_request_async", lambda *_a, **_kw: None)

    window = MainWindow(demo_mode=True)
    added: list[str] = []
    monkeypatch.setattr(
        window.toast_overlay,
        "add_toast",
        lambda toast: added.append(toast.get_title()),
    )

    window.show_toast("Applied 'Desktop mouse': 12 mappings filled")

    assert added == ["Applied 'Desktop mouse': 12 mappings filled"]
    window.destroy()
