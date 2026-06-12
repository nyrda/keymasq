import pytest

from tests.gui.support import collect_widgets, iter_widget_children

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")


def test_create_combo_summary_row_supports_readonly_and_trailing_widget() -> None:
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, ComboEvent, ComboStep, MappingAction
    from keymasq.gui.widgets.combo_presentation import create_combo_summary_row

    trailing = Gtk.Button(icon_name="user-trash-symbolic")
    row = create_combo_summary_row(
        name="Quick Save",
        subtitle="Profile: Overlay",
        steps=[
            ComboStep(
                events=[
                    ComboEvent(evdev="key_leftctrl", hardware_id="1234:5678"),
                    ComboEvent(evdev="key_s", hardware_id="1234:5678"),
                ]
            ),
            ComboStep(
                events=[ComboEvent(evdev="key_f6", hardware_id="1234:5678")],
                timeout_ms=600,
            ),
        ],
        action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
        read_only=True,
        tooltip="Combo details",
        step_tooltips=["Keyboard chord", "Follow-up key"],
        trailing_widget=trailing,
    )

    assert row.get_selectable() is False
    assert row.get_tooltip_text() == "Combo details"

    content = row.get_child()
    assert set(content.get_css_classes()) >= {"combo-row", "combo-row-readonly"}
    assert list(iter_widget_children(content))[-1] is trailing

    labels = collect_widgets(content, Gtk.Label)
    assert [label.get_text() for label in labels[:5]] == [
        "Quick Save",
        "Profile: Overlay",
        "Ctrl+S",
        "\u2192",
        "F6",
    ]
    assert labels[-1].get_text().endswith("key_f5")

    pills = [
        label
        for label in labels
        if "combo-step-pill" in set(label.get_css_classes())
    ]
    assert [pill.get_tooltip_text() for pill in pills] == ["Keyboard chord", "Follow-up key"]
