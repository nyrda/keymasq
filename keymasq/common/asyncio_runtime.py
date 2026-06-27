import asyncio
import logging
from importlib import import_module
from types import ModuleType

log = logging.getLogger("keymasq.asyncio")

_STATUS_UNKNOWN = "unknown"
_STATUS_UVLOOP = "uvloop"
_STATUS_DEFAULT = "asyncio-default"

_runtime_status = _STATUS_UNKNOWN
_runtime_detail = ""
_logged_statuses: set[str] = set()


def _uvloop_module() -> ModuleType:
    return import_module("uvloop")


def ensure_uvloop(logger: logging.Logger | None = None) -> bool:
    global _runtime_status, _runtime_detail

    if _runtime_status == _STATUS_UNKNOWN:
        try:
            uvloop = _uvloop_module()
            current_policy = asyncio.get_event_loop_policy()  # type: ignore[reportDeprecated]
            uvloop_policy_type = uvloop.EventLoopPolicy
            if not isinstance(current_policy, uvloop_policy_type):
                asyncio.set_event_loop_policy(uvloop_policy_type())  # type: ignore[reportDeprecated]
            _runtime_status = _STATUS_UVLOOP
            _runtime_detail = "uvloop.EventLoopPolicy installed"
        except (ImportError, AttributeError, RuntimeError) as exc:
            _runtime_status = _STATUS_DEFAULT
            _runtime_detail = str(exc).strip() or exc.__class__.__name__
        except Exception as exc:
            _runtime_status = _STATUS_DEFAULT
            _runtime_detail = str(exc).strip() or exc.__class__.__name__
            log.exception("Unexpected failure configuring uvloop")

    if logger is not None and _runtime_status not in _logged_statuses:
        if _runtime_status == _STATUS_UVLOOP:
            logger.info("Using uvloop as default asyncio event loop policy")
        else:
            logger.warning(
                "uvloop unavailable, using default asyncio event loop policy: %s",
                _runtime_detail,
            )
        _logged_statuses.add(_runtime_status)

    return _runtime_status == _STATUS_UVLOOP


def current_asyncio_runtime() -> str:
    if _runtime_status == _STATUS_UNKNOWN:
        ensure_uvloop()
    return _runtime_status
