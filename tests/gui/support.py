import copy
from collections.abc import Callable

SessionPayload = dict[str, object]
SessionCallback = Callable[[SessionPayload], object]
SessionRequestHandler = Callable[[SessionPayload, SessionCallback, float], object]


class SessionIpcHarness:
    def __init__(self, request_handler: SessionRequestHandler | None = None) -> None:
        self.callbacks: dict[str, list[SessionCallback]] = {}
        self.unregistered: list[tuple[str, SessionCallback]] = []
        self.requests: list[SessionPayload] = []
        self.request_timeouts: list[float] = []
        self.response_callbacks: list[SessionCallback] = []
        self._request_handler = request_handler

    def install(self, monkeypatch, module) -> "SessionIpcHarness":
        monkeypatch.setattr(module, "register_session_event_callback", self.register)
        monkeypatch.setattr(module, "unregister_session_event_callback", self.unregister)
        monkeypatch.setattr(module, "session_request_async", self.request_async)
        return self

    def register(self, event: str, callback: SessionCallback) -> None:
        self.callbacks.setdefault(event, []).append(callback)

    def unregister(self, event: str, callback: SessionCallback) -> None:
        self.unregistered.append((event, callback))
        registered = self.callbacks.get(event, [])
        if callback in registered:
            registered.remove(callback)

    def request_async(
        self,
        payload: SessionPayload,
        callback: SessionCallback,
        timeout: float = 5.0,
    ) -> object:
        request = copy.deepcopy(payload)
        self.requests.append(request)
        self.request_timeouts.append(timeout)
        if self._request_handler is not None:
            return self._request_handler(request, callback, timeout)
        self.response_callbacks.append(callback)
        return None

    def emit(self, event: str, payload: SessionPayload | None = None) -> list[object]:
        message: SessionPayload = {"event": event}
        if payload is not None:
            message.update(payload)
        return [callback(message) for callback in list(self.callbacks.get(event, []))]

    def respond(self, index: int, payload: SessionPayload) -> object:
        return self.response_callbacks[index](payload)


def iter_widget_children(widget):
    if widget is None:
        return
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


def iter_widget_tree(widget, *, include_self: bool = False):
    if widget is None:
        return
    if include_self:
        yield widget
    for child in iter_widget_children(widget):
        yield child
        yield from iter_widget_tree(child)


def collect_child_widgets(widget, widget_type):
    return [child for child in iter_widget_children(widget) if isinstance(child, widget_type)]


def collect_widgets(widget, widget_type, *, include_self: bool = False):
    return [
        child
        for child in iter_widget_tree(widget, include_self=include_self)
        if isinstance(child, widget_type)
    ]


def _first_widget_label(widget):
    for child in iter_widget_tree(widget, include_self=True):
        get_label = getattr(child, "get_label", None)
        if callable(get_label):
            return get_label()
    raise AssertionError("widget tree does not contain a label")


def collect_listbox_row_labels(listbox):
    return [_first_widget_label(row.get_child()) for row in iter_widget_children(listbox)]


__all__ = [
    "SessionCallback",
    "SessionIpcHarness",
    "SessionPayload",
    "SessionRequestHandler",
    "iter_widget_children",
    "collect_child_widgets",
    "collect_listbox_row_labels",
    "collect_widgets",
    "iter_widget_tree",
]
