import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from keymasq.common.virtual_device_templates import LOGITECH_EXTREME_3D_TEMPLATE  # noqa: E402
from keymasq.gui.widgets.virtual_device_picker import VirtualDevicePicker  # noqa: E402


def buttons(widget):
    if isinstance(widget, Gtk.Button):
        yield widget
    child = widget.get_first_child()
    while child:
        yield from buttons(child)
        child = child.get_next_sibling()


@pytest.fixture
def picker():
    selected = []
    widget = VirtualDevicePicker(
        LOGITECH_EXTREME_3D_TEMPLATE,
        lambda button, code: selected.append((code,)),
        lambda button, code, value: selected.append((code, value)),
    )
    return widget, selected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Trigger", ("btn_trigger",)),
        ("12", ("btn_base6",)),
        ("↶ Left", ("abs_rz", 0)),
        ("Right ↷", ("abs_rz", 255)),
        ("Idle", ("abs_throttle", 255)),
        ("Half", ("abs_throttle", 128)),
        ("Full", ("abs_throttle", 0)),
    ],
)
def test_flight_stick_shortcuts_emit_expected_controls(picker, label, expected):
    widget, selected = picker
    next(button for button in buttons(widget) if button.get_label() == label).emit("clicked")
    assert selected == [expected]


def test_direction_pads_keep_stick_and_hat_axes_distinct(picker):
    widget, selected = picker
    for button in buttons(widget):
        if button.get_label() in {"↑", "↓", "←", "→"}:
            button.emit("clicked")
    assert set(selected) == {
        ("abs_x", 0),
        ("abs_x", 1023),
        ("abs_y", 0),
        ("abs_y", 1023),
        ("abs_hat0x", -1),
        ("abs_hat0x", 1),
        ("abs_hat0y", -1),
        ("abs_hat0y", 1),
    }


@pytest.mark.parametrize(
    ("axis", "percent", "value"),
    [
        ("abs_x", -100, 0),
        ("abs_x", 0, 511),
        ("abs_x", 100, 1023),
        ("abs_rz", 0, 127),
        ("abs_throttle", 0, 255),
        ("abs_throttle", 100, 0),
    ],
)
def test_axis_editor_maps_percentage_using_target_rest(picker, axis, percent, value):
    widget, selected = picker
    widget.axis_choice.set_selected(widget.axis_names.index(axis))
    widget.percent_spin.set_value(percent)
    next(button for button in buttons(widget) if button.get_label() == "Map axis").emit("clicked")
    assert selected == [(axis, value)]


def test_reopening_exact_axis_value_does_not_round_it_to_a_percentage():
    selected = []
    widget = VirtualDevicePicker(
        LOGITECH_EXTREME_3D_TEMPLATE,
        lambda *args: None,
        lambda button, code, value: selected.append((code, value)),
        current_target="abs_x",
        current_value=673,
    )
    next(button for button in buttons(widget) if button.get_label() == "Map axis").emit("clicked")
    assert selected == [("abs_x", 673)]


@pytest.mark.parametrize("layout", ["flight-stick", "gamepad"])
def test_custom_template_layout_maps_only_configured_buttons(picker, layout):
    from dataclasses import replace

    from keymasq.common.virtual_device_templates import VirtualButton

    template = replace(
        LOGITECH_EXTREME_3D_TEMPLATE,
        id="panel",
        builtin=False,
        layout=layout,
        buttons=(VirtualButton("gear", "Landing gear", "btn_trigger_happy1"),),
        axes=tuple(
            axis
            for axis in LOGITECH_EXTREME_3D_TEMPLATE.axes
            if axis.evdev in {"abs_x", "abs_y", "abs_rz"}
        ),
    )
    selected = []
    widget = VirtualDevicePicker(
        template, lambda button, code: selected.append(code), lambda *args: None
    )
    all_buttons = list(buttons(widget))
    assert not any(
        button.get_label() in {"Trigger", "Thumb", "A", "Start"} for button in all_buttons
    )
    next(button for button in all_buttons if button.get_label() == "1 · Landing gear").emit(
        "clicked"
    )
    assert selected == ["btn_trigger_happy1"]
    widget.extra_search.set_text("landing")
    assert widget._extra_matches(widget._extra_items[0][0])
    widget.extra_search.set_text("missing")
    widget._filter_extras()
    assert not widget._extra_matches(widget._extra_items[0][0])
    assert widget._no_results.get_visible()


def test_custom_flight_shortcuts_use_edited_ranges():
    from dataclasses import replace

    template = replace(
        LOGITECH_EXTREME_3D_TEMPLATE,
        id="custom-flight",
        builtin=False,
        axes=tuple(
            replace(axis, minimum=-1000, maximum=1000, rest=0) if axis.evdev == "abs_rz" else axis
            for axis in LOGITECH_EXTREME_3D_TEMPLATE.axes
        ),
    )
    selected = []
    widget = VirtualDevicePicker(
        template, lambda *args: None, lambda button, code, value: selected.append((code, value))
    )
    next(button for button in buttons(widget) if button.get_label() == "↶ Left").emit("clicked")
    next(button for button in buttons(widget) if button.get_label() == "Right ↷").emit("clicked")
    assert selected == [("abs_rz", -1000), ("abs_rz", 1000)]
