from unittest.mock import MagicMock

import evdev

from keymasq.keymasqd import output_helpers


def test_resolve_output_code_handles_known_tuple_and_unknown_targets(monkeypatch) -> None:
    monkeypatch.setattr(output_helpers.evdev.ecodes, "FAKE_TUPLE", (123, "fake"), raising=False)
    monkeypatch.setattr(output_helpers.evdev.ecodes, "fake_lower", (456, "fake"), raising=False)

    assert output_helpers.resolve_output_code(None) is None
    assert output_helpers.resolve_output_code("btn_left") == evdev.ecodes.BTN_LEFT
    assert output_helpers.resolve_output_code("fake_tuple") == 123
    assert output_helpers.resolve_output_code("fake_lower") == 456
    assert output_helpers.resolve_output_code("missing_code") is None


def test_get_trigger_axis_maps_aliases() -> None:
    assert output_helpers.get_trigger_axis(None) == (False, None)
    assert output_helpers.get_trigger_axis("btn_tl2") == (True, evdev.ecodes.ABS_Z)
    assert output_helpers.get_trigger_axis("btn_lt") == (True, evdev.ecodes.ABS_Z)
    assert output_helpers.get_trigger_axis("btn_tr2") == (True, evdev.ecodes.ABS_RZ)
    assert output_helpers.get_trigger_axis("btn_rt") == (True, evdev.ecodes.ABS_RZ)
    assert output_helpers.get_trigger_axis("key_a") == (False, None)


def test_emit_mouse_move_supports_absolute_mode_and_swallows_errors() -> None:
    uinput = MagicMock()

    output_helpers.emit_mouse_move(uinput, 12, -7, absolute=True)

    writes = [tuple(call.args) for call in uinput.write.call_args_list]
    assert writes == [
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, -2147483648),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, -2147483648),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 12),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, -7),
    ]
    assert uinput.syn.call_count == 2

    broken = MagicMock()
    broken.write.side_effect = RuntimeError("boom")
    output_helpers.emit_mouse_move(broken, 1, 2)
