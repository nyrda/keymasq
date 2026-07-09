from keymasq import __version__
from keymasq.common.slurp import get_slurp_capture
from keymasq.gui.compositor_state import session_compositor_id
from keymasq.gui.widgets.analog_control import dialog as _dialog
from keymasq.gui.widgets.analog_control.compat import (
    analog_controls_docs_url as _analog_controls_docs_url,
)
from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog
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
from keymasq.session.hardware import HardwareManager

Adw = _dialog.Adw
Gdk = _dialog.Gdk
GLib = _dialog.GLib
GObject = _dialog.GObject
Gtk = _dialog.Gtk

__all__ = [
    "AnalogControlDialog",
    "Adw",
    "Gdk",
    "GLib",
    "GObject",
    "Gtk",
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
    "get_slurp_capture",
    "session_compositor_id",
    "HardwareManager",
    "virtual_gamepad_count",
    "__version__",
]
