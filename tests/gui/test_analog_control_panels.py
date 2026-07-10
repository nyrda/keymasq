import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk  # noqa: E402

from keymasq.gui.widgets.analog_control.gamepad import (  # noqa: E402
    GamepadPanelCallbacks,
    build_gamepad_output_group,
)
from keymasq.gui.widgets.analog_control.layout import (  # noqa: E402
    DigitalPanelCallbacks,
    TemplatePanelCallbacks,
    build_digital_group,
    build_template_group,
)
from keymasq.gui.widgets.analog_control.mouse import (  # noqa: E402
    MousePanelConfig,
    build_mouse_group,
)
from keymasq.gui.widgets.position_capture import PositionCaptureController  # noqa: E402
from keymasq.gui.widgets.spin_inputs import (  # noqa: E402
    CompactIntEntryController,
    SplitAxisDesyncController,
)


class _SlurpCapture:
    def capture_point(self, _callback) -> None:
        pass


def test_mouse_panel_operates_with_only_its_explicit_dependencies() -> None:
    events: list[str] = []
    panel = build_mouse_group(
        MousePanelConfig(
            capture=PositionCaptureController(
                slurp_capture=_SlurpCapture(),
                slurp_available=True,
            ),
            int_entries=CompactIntEntryController(lambda: events.append("entry")),
            split_desync=SplitAxisDesyncController(),
            modified=lambda: events.append("modified"),
            split_speed_changed=lambda _row, axis: events.append(f"speed:{axis}"),
            area_radius_changed=lambda _row, axis: events.append(f"radius:{axis}"),
            curve_changed=lambda: events.append("curve"),
            invert_axis_toggled=lambda _button, axis: events.append(f"invert:{axis}"),
            area_start_enabled_changed=lambda: events.append("area-enabled"),
            begin_capture=lambda: events.append("capture"),
        )
    )

    panel.speed_x_row.set_value(1000)
    panel.area_radius_y_row.set_value(500)
    panel.deadzone_row.set_value(0.2)
    panel.invert_x_btn.set_active(True)
    panel.area_start_enabled_row.set_active(True)
    panel.area_start_capture_btn.emit("clicked")

    assert {
        "modified",
        "speed:x",
        "radius:y",
        "curve",
        "invert:x",
        "area-enabled",
        "capture",
    }.issubset(events)


def test_gamepad_panel_operates_with_only_its_explicit_callbacks() -> None:
    events: list[str] = []
    panel = build_gamepad_output_group(
        GamepadPanelCallbacks(
            modified=lambda: events.append("modified"),
            output_selected=lambda _dropdown: events.append("output"),
            direction_toggled=lambda button: (
                events.append("direction") if button.get_active() else None
            ),
            curve_changed=lambda: events.append("curve"),
        )
    )

    panel.gamepad_output_dropdown.set_model(Gtk.StringList.new(["One", "Two"]))
    panel.gamepad_output_dropdown.set_selected(1)
    panel.gamepad_output_deadzone_row.set_value(10)
    panel.gamepad_output_direction_min_btn.set_active(True)
    panel.gamepad_output_invert_x_btn.set_active(True)

    assert {"modified", "output", "direction", "curve"}.issubset(events)


def test_digital_and_template_panels_dispatch_explicit_actions() -> None:
    events: list[str] = []
    digital = build_digital_group(
        DigitalPanelCallbacks(add_range=lambda: events.append("add"))
    )
    templates = build_template_group(
        TemplatePanelCallbacks(
            apply_wasd=lambda: events.append("wasd"),
            apply_arrows=lambda: events.append("arrows"),
            apply_mouse_wheel=lambda: events.append("wheel"),
        )
    )

    digital.add_range_row.emit("activated")
    for row in templates.action_rows:
        row.emit("activated")

    assert events == ["add", "wasd", "arrows", "wheel"]
