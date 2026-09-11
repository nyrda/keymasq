import json
from unittest.mock import MagicMock

import evdev
import pytest

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.gui.widgets.macro_editor import selection, timing_ops
from keymasq.gui.widgets.macro_editor.document import MacroDocument
from keymasq.gui.widgets.macro_editor.model import EditableEvent, parse_events, reconstruct_events
from tests.gui.macro_editor_dialog_support import _build_macro_dialog


def rapidfire() -> EditableEvent:
    return EditableEvent(
        "gamepad",
        evdev.ecodes.EV_KEY,
        evdev.ecodes.BTN_SOUTH,
        50_000,
        150_000,
        output_id="virtual-gamepad-3",
        rapidfire_enabled=True,
        rapidfire_hold_ms=10,
        rapidfire_wait_ms=10,
    )


def test_save_reopen_and_copy_keep_one_routed_rapidfire_block() -> None:
    event = rapidfire()
    lists = ([event], [], [], [], [])
    raw = reconstruct_events(*lists)
    assert len(raw) == 1
    assert raw[0]["macro_action"] == "macro_rapidfire"
    assert raw[0]["duration_us"] == 100_000
    document = MacroDocument.from_payload({"events": raw})
    assert document.duration_us == 150_000
    assert len(document.events) == 1
    reopened = document.events[0]
    assert reopened.rapidfire_enabled
    assert reopened.rapidfire_hold_ms == reopened.rapidfire_wait_ms == 10
    assert reopened.output_id == "virtual-gamepad-3"
    assert reconstruct_events(*parse_events(raw)) == raw

    selection.assign_missing_orders(lists)
    fragment = selection.Fragment.capture(lists, [event])
    copied = selection.Fragment.from_clipboard(json.dumps(fragment.clipboard_payload()).encode())
    pasted = copied.paste(lists, 200_000)
    assert len(pasted) == 1
    assert isinstance(pasted[0], EditableEvent)
    assert pasted[0].rapidfire_enabled
    assert pasted[0].output_id == "virtual-gamepad-3"
    assert selection.bounds(pasted) == (200_000, 300_000)


def test_duration_edits_keep_rapidfire_preferences_and_recompute_the_block() -> None:
    event = rapidfire()
    lists = ([event], [], [], [], [])
    selection.scale([event], 2)
    assert (event.press_t_us, event.release_t_us) == (50_000, 250_000)
    assert event.rapidfire_hold_ms == event.rapidfire_wait_ms == 10
    timing_ops.trim_endpoint(*lists, 175_000)
    raw = reconstruct_events(*lists)
    assert len(raw) == 1
    assert raw[0]["duration_us"] == 125_000


def test_edit_reopens_shared_selector_with_saved_rapidfire_settings(monkeypatch) -> None:
    from keymasq.gui.widgets.key_selector import dialog as selector_module

    factory = MagicMock()
    monkeypatch.setattr(selector_module, "KeySelectorDialog", factory)
    dialog = _build_macro_dialog(monkeypatch)
    event = rapidfire()
    dialog._events = [event]
    dialog._timeline._selected = event
    dialog._on_change_key_clicked(None)
    action = factory.call_args.args[2]
    assert isinstance(action, MappingAction)
    assert action.action_type == ActionType.GAMEPAD
    assert action.output_id == "virtual-gamepad-3"
    assert action.rapidfire_enabled
    assert action.rapidfire_hold_ms == action.rapidfire_wait_ms == 10
    assert factory.call_args.kwargs["allow_rapidfire"] is True
    assert factory.call_args.kwargs["allow_tap"] is False
    factory.return_value.connect.assert_called_once_with(
        "key-selected", dialog._on_key_selected_for_edit
    )


@pytest.mark.parametrize(
    ("action_type", "target", "device_type"),
    [
        (ActionType.KEYBOARD, "key_a", "keyboard"),
        (ActionType.MOUSE, "btn_left", "mouse"),
        (ActionType.GAMEPAD, "btn_south", "gamepad"),
    ],
)
def test_selector_result_creates_edits_and_disables_rapidfire(
    monkeypatch,
    action_type: ActionType,
    target: str,
    device_type: str,
) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    action = MappingAction(
        action_type=action_type,
        target=target,
        output_id="virtual-gamepad-3",
        rapidfire_enabled=True,
        rapidfire_hold_ms=10,
        rapidfire_wait_ms=10,
    )
    dialog._on_key_selected_for_insert(dialog, action, 50_000)
    event = dialog._events[0]
    assert event.device_type == device_type
    assert event.rapidfire_enabled
    assert "Rapidfire" in dialog._prop_title.get_label()
    dialog._duration_spin.set_value(100)
    assert event.release_t_us == 150_000
    assert "6 pulses" in dialog._key_info_label.get_label()
    assert "8.000 ms" in dialog._key_info_label.get_label()

    action.rapidfire_hold_ms = 5
    dialog._on_key_selected_for_edit(dialog, action)
    assert len(dialog._events) == 1
    assert event.rapidfire_hold_ms == 5
    assert event.release_t_us == 150_000

    action.rapidfire_enabled = False
    dialog._on_key_selected_for_edit(dialog, action)
    assert not event.rapidfire_enabled
    raw = reconstruct_events(dialog._events, [], [], [], [])
    assert [(e["t_us"], e["value"]) for e in raw] == [(50_000, 1), (150_000, 0)]
