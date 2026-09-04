"""Widget-free tests for macro manager catalog and recording state."""

import pytest

from keymasq.gui.widgets.macro_manager.state import (
    CatalogState,
    CatalogValidationError,
    MacroRowState,
    RecordingState,
    suggest_duplicate_macro_name,
    suggest_unique_macro_name,
)


def test_catalog_names_include_saved_macros_and_temporary_slots() -> None:
    catalog = CatalogState.from_response(
        {
            "status": "ok",
            "macros": [
                {"name": "saved", "device_types": ["keyboard"]},
                {
                    "name": "recording-slot-2",
                    "kind": "recording_slot",
                    "recording_slot": 2,
                },
            ],
        }
    )

    assert catalog.names == {"saved", "recording-slot-2"}


def test_catalog_validation_reports_the_invalid_entry() -> None:
    with pytest.raises(CatalogValidationError, match="index 1"):
        CatalogState.from_response({"macros": [{"name": "valid"}, {"duration_us": 1000}]})


def test_search_filters_macros_and_empty_query_restores_all() -> None:
    catalog = CatalogState.from_response(
        {
            "macros": [
                {
                    "name": "copy_ctrl_c",
                    "device_types": ["keyboard"],
                    "event_count": 4,
                },
                {
                    "name": "gamepad_combo",
                    "device_types": ["gamepad"],
                    "event_count": 2,
                },
            ]
        }
    )
    catalog.query = "gamepad"

    assert [macro["name"] for macro in catalog.filtered_macros()] == ["gamepad_combo"]
    catalog.query = ""
    assert [macro["name"] for macro in catalog.filtered_macros()] == [
        "copy_ctrl_c",
        "gamepad_combo",
    ]


def test_row_state_formats_saved_and_temporary_metadata() -> None:
    saved = MacroRowState.from_macro(
        {
            "name": "saved",
            "duration_us": 1_500_000,
            "device_types": ["keyboard", "mouse"],
            "event_count": 4,
        }
    )
    temporary = MacroRowState.from_macro(
        {
            "name": "slot",
            "kind": "recording_slot",
            "duration_us": 250_000,
        }
    )

    assert saved.metadata == "1.5s · kbd+mouse · 4 events"
    assert temporary.metadata == "temporary · 250ms"
    assert temporary.is_temporary_slot is True


def test_recording_state_keeps_active_slot_stable() -> None:
    state = RecordingState(enabled=True, selected_slot=3)
    request = state.next_request()

    assert request is not None
    assert request.command == "start_recording"
    assert request.slot == 3
    assert state.active_slot == 3

    state.active = True
    assert state.select_index(1, max_slots=4) == 2
    stop = state.next_request()
    assert stop is not None
    assert stop.command == "stop_recording"
    assert stop.slot == 3

    state.recording_stopped()
    assert state.active is False
    assert state.active_slot == 0


def test_name_suggestions_are_deterministic() -> None:
    assert suggest_unique_macro_name({"macro", "macro_1"}) == "macro_2"
    assert suggest_duplicate_macro_name("copy", {"copy_1", "copy_2"}) == "copy_3"
