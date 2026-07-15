"""Widget-free tests for macro editor document and transition state."""

import evdev

from keymasq.gui.widgets.macro_editor.document import (
    CloseAction,
    MacroDocument,
    SaveMode,
    close_action,
    close_response_action,
    has_pending_changes,
    is_valid_macro_name,
    resolve_save_target,
)


def test_macro_document_parses_and_serializes_without_dialog() -> None:
    source = {
        "name": "recorded",
        "revision": 4,
        "events": [
            {
                "device_type": "keyboard",
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_A,
                "value": 1,
                "t_us": 1_000,
            },
            {
                "device_type": "keyboard",
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_A,
                "value": 0,
                "t_us": 5_000,
            },
        ],
        "duration_us": 3_000,
        "loop_mode": "count",
        "loop_count": 2,
        "loop_stop_behavior": "cancel_run",
        "block_mouse_movement": False,
    }

    document = MacroDocument.from_payload(source)
    payload = document.to_payload(
        "edited",
        loop_mode="hold",
        loop_count=1,
        loop_stop_behavior="finish_run",
        move_to_start=False,
        start_x=0,
        start_y=0,
        block_mouse_movement=True,
    )

    assert len(document.events) == 1
    assert document.duration_us == 5_000
    assert payload["name"] == "edited"
    assert payload["revision"] == 4
    assert payload["duration_us"] == 5_000
    assert payload["loop_mode"] == "hold"
    assert payload["loop_stop_behavior"] == "finish_run"
    assert payload["block_mouse_movement"] is True
    assert "move_to_start" not in payload


def test_dirty_comparison_ignores_device_type_order() -> None:
    initial = {"name": "demo", "device_types": ["mouse", "keyboard"]}
    reordered = {"name": "demo", "device_types": ["keyboard", "mouse"]}

    assert not has_pending_changes(
        initial_state_loaded=True,
        initial_payload=initial,
        current_payload=reordered,
    )
    assert has_pending_changes(
        initial_state_loaded=True,
        initial_payload=initial,
        current_payload={**reordered, "name": "renamed"},
    )


def test_close_flow_is_widget_independent() -> None:
    assert close_action(False) is CloseAction.CLOSE
    assert close_action(True) is CloseAction.PROMPT
    assert close_response_action("save") is CloseAction.SAVE
    assert close_response_action("discard") is CloseAction.DISCARD
    assert close_response_action("cancel") is CloseAction.CANCEL


def test_save_target_resolves_create_update_and_rename() -> None:
    created = resolve_save_target(
        macro_exists=False,
        current_name="draft",
        requested_name="draft",
        revision=1,
    )
    updated = resolve_save_target(
        macro_exists=True,
        current_name="saved",
        requested_name="saved",
        revision=7,
    )
    renamed = resolve_save_target(
        macro_exists=True,
        current_name="saved",
        requested_name="renamed",
        revision=7,
    )

    assert created.mode is SaveMode.CREATE
    assert updated.mode is SaveMode.UPDATE
    assert renamed.mode is SaveMode.RENAME
    assert renamed.current_name == "saved"
    assert renamed.revision == 7


def test_macro_name_validation_is_widget_independent() -> None:
    assert is_valid_macro_name("valid-name_2")
    assert not is_valid_macro_name("")
    assert not is_valid_macro_name("contains spaces")
