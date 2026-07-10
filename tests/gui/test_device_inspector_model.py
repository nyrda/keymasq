from collections.abc import Callable

import pytest

from keymasq.gui.widgets.device_inspector.model import (
    EventHistory,
    Payload,
    event_category,
    normalize_axis,
)
from keymasq.gui.widgets.device_inspector.session import InspectorSession


def test_event_history_bounds_categories_and_preserves_global_order() -> None:
    history = EventHistory(limit=2)
    for sequence in range(1, 4):
        history.add(
            {
                "sequence": sequence,
                "type_name": "ev_key",
                "code_name": "key_a",
                "value": 1,
            }
        )
    history.add(
        {
            "sequence": 4,
            "type_name": "ev_rel",
            "code_name": "rel_x",
            "value": 7,
        }
    )

    assert [event["sequence"] for event in history.by_category["button"]] == [3, 2]
    assert [event["sequence"] for event in history.visible({"button", "mousemove"})] == [
        4,
        3,
    ]
    assert history.export({"button"}) == ("#3 key_a ev_key value=1\n#2 key_a ev_key value=1")


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"type_name": "ev_key"}, "button"),
        ({"type_name": "ev_abs"}, "axis"),
        ({"type_name": "ev_rel", "code_name": "rel_x"}, "mousemove"),
        ({"type": 4, "code": 4}, "syn"),
        ({"type_name": "unexpected"}, "other"),
    ],
)
def test_event_category_is_pure(event: Payload, expected: str) -> None:
    assert event_category(event) == expected


def test_axis_normalization_uses_independent_sides_and_trigger_rest() -> None:
    stick = {"minimum": -100, "maximum": 300, "center": 0}
    trigger = {"minimum": 0, "maximum": 255}

    assert normalize_axis(stick, -50, "stick") == pytest.approx(-0.5)
    assert normalize_axis(stick, 150, "stick") == pytest.approx(0.5)
    assert normalize_axis(trigger, 128, "axis") == pytest.approx(128 / 255)


def test_inspector_session_owns_registration_requests_and_idempotent_stop() -> None:
    requests: list[tuple[Payload, float]] = []
    registered: list[tuple[str, Callable[[Payload], bool | None]]] = []
    unregistered: list[tuple[str, Callable[[Payload], bool | None]]] = []

    def request(
        payload: Payload,
        _callback: Callable[[Payload | None], bool | None],
        timeout: float = 5.0,
    ) -> None:
        requests.append((dict(payload), timeout))

    def register(name: str, callback: Callable[[Payload], bool | None]) -> None:
        registered.append((name, callback))

    def unregister(name: str, callback: Callable[[Payload], bool | None]) -> None:
        unregistered.append((name, callback))

    def on_event(_event: Payload) -> bool:
        return False

    def on_response(_response: Payload | None) -> bool:
        return False

    session = InspectorSession("1234:5678", request, register, unregister)
    session.start({"device_inspector_event": on_event}, on_response)
    session.request_snapshot(on_response)
    session.set_suppressed(True, on_response)
    session.set_suppressed(False, on_response, reason="key_esc")

    assert registered == [("device_inspector_event", on_event)]
    assert [payload["command"] for payload, _timeout in requests] == [
        "start_device_inspector",
        "get_device_inspector_snapshot",
        "enable_device_inspector_suppression",
        "disable_device_inspector_suppression",
    ]
    assert requests[-1][0]["reason"] == "key_esc"

    assert session.finalize() is True
    assert session.finalize() is False
    assert unregistered == registered
    assert [payload["command"] for payload, _timeout in requests].count(
        "stop_device_inspector"
    ) == 1
