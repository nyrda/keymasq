from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from keymasq.common.model.hardware import AnalogAxisDefinition, AnalogInputDefinition
from keymasq.gui.widgets.device_tab.analog_edit_dialog import AnalogInputEditDialog
from keymasq.gui.widgets.device_tab.inputs import InputInventoryMixin


def make_analog(kind="stick"):
    return AnalogInputDefinition(
        id="existing-control",
        label="Stick",
        type=kind,
        source="joystick",
        axes=[
            AnalogAxisDefinition(
                role=role,
                evdev=code,
                evdev_code=index,
                minimum=0,
                maximum=1023,
                center=511 if kind == "stick" else None,
                rest=255 if kind == "axis" else None,
                invert=True,
            )
            for index, (role, code) in enumerate(
                [("x", "abs_x"), ("y", "abs_y")] if kind == "stick" else [("x", "abs_throttle")]
            )
        ],
    )


def make_dialog(analog, saved):
    return AnalogInputEditDialog(
        analog,
        on_save=lambda edited: saved.append(edited) or True,
        on_delete_clicked=lambda *args: None,
        on_close_clicked=lambda button, dialog: dialog.close(),
    )


@pytest.mark.parametrize("kind,neutral_field", [("stick", "center"), ("axis", "rest")])
def test_edit_calibration_preserves_source_and_identity(kind, neutral_field):
    analog = make_analog(kind)
    original = deepcopy(analog)
    saved = []
    dialog = make_dialog(analog, saved)
    dialog.name_entry.set_text("Adjusted control")
    row = dialog.axis_entries[0]
    row["minimum"].set_text("-100")
    row["maximum"].set_text("900")
    row[neutral_field].set_text("300")
    assert analog == original
    dialog.save_button.emit("clicked")
    assert len(saved) == 1
    edited = saved[0]
    assert (edited.id, edited.source, edited.type) == (analog.id, analog.source, kind)
    assert edited.label == "Adjusted control"
    assert (edited.axes[0].minimum, edited.axes[0].maximum) == (-100, 900)
    assert getattr(edited.axes[0], neutral_field) == 300
    assert edited.axes[0].evdev == analog.axes[0].evdev
    assert edited.axes[0].invert is True
    if kind == "stick":
        assert edited.axes[1] == original.axes[1]
    assert analog == original


def test_blank_calibration_removes_overrides():
    saved = []
    dialog = make_dialog(make_analog(), saved)
    for row in dialog.axis_entries:
        for entry in row.values():
            entry.set_text("")
    dialog.save_button.emit("clicked")
    assert all(a.minimum is None and a.maximum is None and a.center is None for a in saved[0].axes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("minimum", "1023"),
        ("maximum", "-1"),
        ("center", "1024"),
        ("minimum", "1.5"),
        ("maximum", "2147483648"),
    ],
)
def test_invalid_values_do_not_save(field, value):
    analog = make_analog()
    original = deepcopy(analog)
    saved = []
    dialog = make_dialog(analog, saved)
    dialog.axis_entries[0][field].set_text(value)
    dialog.save_button.emit("clicked")
    assert not saved
    assert analog == original
    assert dialog.error_label.get_visible()


def test_cancel_and_failed_save_leave_original_unchanged():
    analog = make_analog()
    original = deepcopy(analog)
    dialog = make_dialog(analog, [])
    dialog.name_entry.set_text("Unsaved")
    dialog.close()
    assert analog == original
    dialog = make_dialog(analog, [])
    dialog._on_save = lambda edited: False
    dialog.save_button.emit("clicked")
    assert dialog.error_label.get_visible()
    assert analog == original


@pytest.mark.parametrize("fail", [False, True])
def test_commit_preserves_mapping_identity_and_rolls_back_failed_writes(fail):
    analog = make_analog()
    original = deepcopy(analog)
    writes, requests = [], []

    def save_hardware(device):
        if fail:
            raise OSError("Cannot write hardware config")
        writes.append(deepcopy(device))

    host = SimpleNamespace(
        hardware_manager=SimpleNamespace(save_hardware=save_hardware),
        device=SimpleNamespace(analog_inputs=[analog]),
        _request_session_async=lambda request, callback: requests.append(request),
        _ignore_session_response=lambda *args: None,
        _button_widgets={},
    )
    edited = replace(analog, label="Adjusted", axes=[replace(a, center=400) for a in analog.axes])
    assert InputInventoryMixin._save_analog_properties(host, edited) is (not fail)
    if fail:
        assert analog == original
        assert requests == []
    else:
        assert writes[0].analog_inputs[0] == edited
        assert analog.id == original.id
        assert requests == [{"command": "reload"}]


def test_automatic_values_use_reported_bounds_and_do_not_save_hints():
    from keymasq.common.model.hardware import HardwareConfig

    analog = make_analog()
    for axis in analog.axes:
        axis.minimum = axis.maximum = axis.center = None
    saved = []
    dialog = make_dialog(analog, saved)
    hardware = HardwareConfig("1234", "5678", "Test", [], [], [analog])
    assert dialog.axis_entries[0]["minimum"].get_placeholder_text() == "Unavailable"
    dialog.update_device_inventory(
        {
            "devices": [
                {
                    "source_hardware_id": hardware.hardware_id,
                    "source_interface_id": analog.source,
                    "abs_info": {"0": {"minimum": 10, "maximum": 810, "value": 750}},
                }
            ]
        },
        hardware,
    )
    row = dialog.axis_entries[0]
    assert row["minimum"].get_placeholder_text() == "10 (auto)"
    assert row["maximum"].get_placeholder_text() == "810 (auto)"
    assert row["center"].get_placeholder_text() == "410 (auto)"
    row["maximum"].set_text("1010")
    assert row["center"].get_placeholder_text() == "510 (auto)"
    row["maximum"].set_text("")
    dialog.save_button.emit("clicked")
    assert saved[0].axes[0].minimum is None
    assert saved[0].axes[0].center is None


def test_automatic_rest_uses_grab_sample_not_current_position():
    from keymasq.common.model.hardware import HardwareConfig

    analog = make_analog("axis")
    analog.axes[0].rest = None
    dialog = make_dialog(analog, [])
    hardware = HardwareConfig("1234", "5678", "Test", [], [], [analog])
    data = {
        "source_hardware_id": hardware.hardware_id,
        "source_interface_id": analog.source,
        "abs_info": {"0": {"minimum": 0, "maximum": 255, "value": 200}},
    }
    dialog.update_device_inventory({"devices": [data]}, hardware)
    assert dialog.axis_entries[0]["rest"].get_placeholder_text() == "On next grab"
    data["analog_calibration"] = {analog.id: {"x": {"rest": 25}}}
    dialog.update_device_inventory({"devices": [data]}, hardware)
    assert dialog.axis_entries[0]["rest"].get_placeholder_text() == "25 (auto)"
    other = dict(data, source_interface_id="another-interface")
    new_dialog = make_dialog(analog, [])
    new_dialog.update_device_inventory({"devices": [other]}, hardware)
    assert new_dialog.axis_entries[0]["rest"].get_placeholder_text() == "On next grab"


@pytest.mark.parametrize("kind,neutral", [("stick", "center"), ("axis", "rest")])
def test_editor_validates_against_detected_bounds_without_saving_them(kind, neutral):
    analog = make_analog(kind)
    for axis in analog.axes:
        axis.minimum = axis.maximum = axis.center = axis.rest = None
    saved = []
    dialog = make_dialog(analog, saved)
    dialog._detected = {"0": {"minimum": 0, "maximum": 255}}
    row = dialog.axis_entries[0]
    row[neutral].set_text("1000")
    dialog.save_button.emit("clicked")
    assert not saved
    assert dialog.error_label.get_visible()
    row[neutral].set_text("100")
    dialog.save_button.emit("clicked")
    assert len(saved) == 1
    assert saved[0].axes[0].minimum is None
    assert saved[0].axes[0].maximum is None
    assert getattr(saved[0].axes[0], neutral) == 100
