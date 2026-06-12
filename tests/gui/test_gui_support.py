from tests.gui.support import SessionIpcHarness


def test_session_ipc_harness_tracks_events_requests_and_unregisters() -> None:
    responses = []

    def request_handler(payload, callback, timeout):
        assert timeout == 1.5
        return callback({"status": "ok", "command": payload["command"]})

    harness = SessionIpcHarness(request_handler=request_handler)
    seen_events = []

    def on_event(event):
        seen_events.append(event)

    harness.register("profiles_changed", on_event)
    harness.emit("profiles_changed", {"profile": "Desktop"})
    harness.request_async({"command": "ping"}, responses.append, timeout=1.5)
    harness.unregister("profiles_changed", on_event)

    assert seen_events == [{"event": "profiles_changed", "profile": "Desktop"}]
    assert responses == [{"status": "ok", "command": "ping"}]
    assert harness.requests == [{"command": "ping"}]
    assert harness.request_timeouts == [1.5]
    assert harness.callbacks["profiles_changed"] == []
    assert harness.unregistered == [("profiles_changed", on_event)]


def test_session_ipc_harness_snapshots_nested_request_payloads() -> None:
    harness = SessionIpcHarness()
    data = {"items": [1]}
    payload = {"command": "save", "data": data}

    harness.request_async(payload, lambda _response: None)
    data["items"].append(2)

    assert harness.requests == [{"command": "save", "data": {"items": [1]}}]
