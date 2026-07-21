import pytest

from tests.gui.support import collect_widgets, iter_widget_children

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")


def test_create_combo_summary_row_supports_readonly_and_trailing_widget() -> None:
    from gi.repository import Gtk

    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.common.model.profiles import ComboEvent, ComboStep
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

    pills = [label for label in labels if "combo-step-pill" in set(label.get_css_classes())]
    assert [pill.get_tooltip_text() for pill in pills] == ["Keyboard chord", "Follow-up key"]


def test_combo_search_does_not_mix_visible_terms_with_hidden_metadata() -> None:
    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.common.model.profiles import ComboConfig, ComboEvent, ComboStep
    from keymasq.gui.widgets.combo_presentation import (
        combo_search_document,
        combo_search_matches,
    )

    documents = []
    for workspace in range(1, 7):
        suffix = f" {workspace}" if workspace > 1 else ""
        combo = ComboConfig(
            id=f"workspace-{workspace}",
            name=f"movetoworkspace{suffix}",
            steps=[
                ComboStep(
                    events=[
                        ComboEvent(
                            evdev=f"key_f{workspace + 4}",
                            hardware_id="shared-device-4",
                            source="kbd",
                        )
                    ]
                )
            ],
            action=MappingAction(
                action_type=ActionType.COMPOSITOR_DISPATCH,
                compositor_dispatcher="movetoworkspace",
                compositor_args=str(workspace),
            ),
        )
        documents.append(combo_search_document(combo, profile_name="Desktop"))

    matching_workspaces = [
        workspace
        for workspace, document in enumerate(documents, start=1)
        if combo_search_matches("movetoworkspace 4", document)
    ]

    assert matching_workspaces == [4]


def test_combo_search_keeps_runtime_device_fields_scoped() -> None:
    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.common.model.profiles import ComboConfig, ComboEvent, ComboStep
    from keymasq.gui.widgets.combo_presentation import (
        combo_search_document,
        combo_search_matches,
    )

    combo = ComboConfig(
        id="quick-save",
        name="Quick Save",
        steps=[
            ComboStep(
                events=[
                    ComboEvent(
                        evdev="key_s",
                        hardware_id="t3-controller",
                        source="kbd",
                    )
                ]
            )
        ],
        action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
    )
    document = combo_search_document(
        combo,
        profile_name="Desktop",
        additional_event_fields=["Test Keyboard", "Test Keyboard kbd"],
    )

    assert combo_search_matches("test kbd", document)
    assert combo_search_matches("quick f5", document)
    assert combo_search_matches("quick desktop", document)
    assert not combo_search_matches("keyboard controller", document)
    assert not combo_search_matches("quick t3", document)
    assert combo_search_matches("qs", document)


def test_combo_row_without_search_document_is_visible_for_an_empty_query() -> None:
    from gi.repository import Gtk

    from keymasq.gui.widgets.combo_presentation import combo_row_search_matches

    row = Gtk.ListBoxRow()

    assert combo_row_search_matches("", row)
    assert not combo_row_search_matches("quick", row)
