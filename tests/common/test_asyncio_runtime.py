import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import keymasq.common.asyncio_runtime as runtime


@pytest.fixture(autouse=True)
def isolate_runtime_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "_runtime_status", runtime._STATUS_UNKNOWN)
    monkeypatch.setattr(runtime, "_runtime_detail", "")
    monkeypatch.setattr(runtime, "_logged_statuses", set())


def test_ensure_uvloop_installs_policy(monkeypatch) -> None:
    installed_policies: list[object] = []

    class _FakePolicy:
        pass

    monkeypatch.setattr(
        runtime,
        "_uvloop_module",
        lambda: SimpleNamespace(EventLoopPolicy=_FakePolicy),
    )
    monkeypatch.setattr(runtime.asyncio, "get_event_loop_policy", lambda: object())
    monkeypatch.setattr(
        runtime.asyncio,
        "set_event_loop_policy",
        lambda policy: installed_policies.append(policy),
    )

    logger = Mock()

    assert runtime.ensure_uvloop(logger) is True
    assert len(installed_policies) == 1
    assert isinstance(installed_policies[0], _FakePolicy)
    logger.info.assert_called_once()
    logger.warning.assert_not_called()


def test_ensure_uvloop_logs_warning_once_when_unavailable(monkeypatch) -> None:
    def _raise_missing():
        raise ModuleNotFoundError("No module named 'uvloop'")

    monkeypatch.setattr(runtime, "_uvloop_module", _raise_missing)

    first_logger = Mock()
    second_logger = Mock()

    assert runtime.ensure_uvloop() is False
    assert runtime.ensure_uvloop(first_logger) is False
    assert runtime.ensure_uvloop(second_logger) is False

    first_logger.warning.assert_called_once()
    second_logger.warning.assert_not_called()


def test_ensure_uvloop_logs_unexpected_setup_failure(monkeypatch, caplog) -> None:
    class _FakePolicy:
        pass

    def _raise_bad_policy(_policy: object) -> None:
        raise ValueError("bad event loop policy")

    monkeypatch.setattr(
        runtime,
        "_uvloop_module",
        lambda: SimpleNamespace(EventLoopPolicy=_FakePolicy),
    )
    monkeypatch.setattr(runtime.asyncio, "get_event_loop_policy", lambda: object())
    monkeypatch.setattr(runtime.asyncio, "set_event_loop_policy", _raise_bad_policy)

    caplog.set_level(logging.ERROR, logger="keymasq.asyncio")

    assert runtime.ensure_uvloop() is False
    assert "Unexpected failure configuring uvloop" in caplog.text
