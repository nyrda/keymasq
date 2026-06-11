from keymasq import __version__
from keymasq.common.slurp import get_slurp_capture
from keymasq.gui.widgets.analog_control import dialog as _dialog
from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog as _AnalogControlDialog
from keymasq.gui.widgets.analog_control.options import (
    _analog_control_search_text,
    _clamp_threshold_value,
    _compute_hysteresis,
    _from_percent,
    _gamepad_output_target_label_for_input_type,
    _gamepad_output_target_options_for_input_type,
    _group_analog_control_names,
    _input_type_index,
    _mode_index_for_input_type,
    _mode_items_for_input_type,
    _mode_labels_for_input_type,
    _mode_options_for_input_type,
    _option_ids,
    _option_index,
    _option_labels,
    _options_for_input_type,
    _SelectOption,
    _to_percent,
)
from keymasq.gui.widgets.gamepad_output_choices import virtual_gamepad_count
from keymasq.session.compositor import detect_compositor_sync
from keymasq.session.hardware import HardwareManager

Adw = _dialog.Adw
Gdk = _dialog.Gdk
GLib = _dialog.GLib
GObject = _dialog.GObject
Gtk = _dialog.Gtk


def _docs_version() -> str:
    version = __version__.strip()
    if not version:
        return "master"
    if "dev" in version:
        return "master"
    return f"v{version.removeprefix('v')}"


def _analog_controls_docs_url() -> str:
    return f"https://keymasq.tools/docs/{_docs_version()}/ANALOG_CONTROLS/"


def _sync_compat_overrides() -> None:
    _dialog.get_slurp_capture = get_slurp_capture
    _dialog.detect_compositor_sync = detect_compositor_sync
    _dialog.virtual_gamepad_count = virtual_gamepad_count
    _dialog.HardwareManager = HardwareManager
    _dialog._analog_controls_docs_url = _analog_controls_docs_url


class AnalogControlDialog(_AnalogControlDialog):
    def __init__(self, *args, **kwargs):
        _sync_compat_overrides()
        super().__init__(*args, **kwargs)


__all__ = [
    "AnalogControlDialog",
    "_SelectOption",
    "_analog_control_search_text",
    "_analog_controls_docs_url",
    "_clamp_threshold_value",
    "_compute_hysteresis",
    "_from_percent",
    "_gamepad_output_target_label_for_input_type",
    "_gamepad_output_target_options_for_input_type",
    "_group_analog_control_names",
    "_input_type_index",
    "_mode_index_for_input_type",
    "_mode_items_for_input_type",
    "_mode_labels_for_input_type",
    "_mode_options_for_input_type",
    "_option_ids",
    "_option_index",
    "_option_labels",
    "_options_for_input_type",
    "_to_percent",
]
