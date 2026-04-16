from collections.abc import Callable

from keymasq.gui.session_client import JsonDict, session_request, session_request_async


def notify_session_reload(timeout: float = 5.0) -> bool:
    try:
        result = session_request({"command": "reload"}, timeout=timeout)
    except Exception:
        return False

    return isinstance(result, dict) and result.get("status") == "ok"


def notify_session_reload_async(
    callback: Callable[[bool], None] | None = None,
    timeout: float = 5.0,
) -> None:
    def _on_result(result: JsonDict | None) -> bool:
        ok = bool(isinstance(result, dict) and result.get("status") == "ok")
        if callback is not None:
            callback(ok)
        return False

    session_request_async({"command": "reload"}, _on_result, timeout=timeout)
