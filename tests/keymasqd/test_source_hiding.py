import asyncio
from pathlib import Path
from typing import Any

import pytest

from keymasq.keymasqd.permission_hints import SOURCE_HIDING_JOB_HINT
from keymasq.keymasqd.runtime import source_hiding


def _configure_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path, Path]:
    hidden_dir = tmp_path / "run" / "keymasq" / "hidden"
    hidden_hardware_dir = tmp_path / "run" / "keymasq" / "hidden-hardware"
    sys_input_dir = tmp_path / "sys" / "class" / "input"
    monkeypatch.setattr(source_hiding, "HIDDEN_DIR", hidden_dir)
    monkeypatch.setattr(source_hiding, "HIDDEN_HARDWARE_DIR", hidden_hardware_dir)
    monkeypatch.setattr(source_hiding, "SYS_CLASS_INPUT", sys_input_dir)
    return hidden_dir, hidden_hardware_dir, sys_input_dir


def _add_js_sibling(sys_input_dir: Path, event_name: str, js_name: str) -> None:
    js_dir = sys_input_dir / event_name / "device" / js_name
    js_dir.mkdir(parents=True)


def _fake_jobs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: BaseException | None = None,
) -> list[dict[str, Any]]:
    """Capture hardware job requests; each entry records operation, timeout, and data."""
    calls: list[dict[str, Any]] = []

    async def fake_request(
        operation: str,
        identity: str,
        *,
        timeout: float,
        **data: object,
    ) -> dict[str, Any]:
        calls.append({"operation": operation, "id": identity, "timeout": timeout, **data})
        if error is not None:
            raise error
        return {"status": "ok"}

    monkeypatch.setattr(source_hiding.hardware_jobs, "request", fake_request)
    return calls


@pytest.mark.asyncio
async def test_hide_source_flags_event_and_js_sibling_and_requests_one_trigger_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    hidden_dir, _hidden_hardware_dir, sys_input_dir = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    _add_js_sibling(sys_input_dir, "event22", "js0")
    calls = _fake_jobs(monkeypatch)

    hidden_names = await source_hiding.hide_source("/dev/input/event22")

    assert hidden_names == ["event22", "js0"]
    assert (hidden_dir / "event22").read_text(encoding="utf-8") == "1\n"
    assert (hidden_dir / "js0").read_text(encoding="utf-8") == "1\n"
    assert calls == [
        {
            "operation": "trigger",
            "id": "",
            "timeout": source_hiding.TRIGGER_TIMEOUT_S,
            "names": ["event22", "js0"],
        }
    ]


@pytest.mark.asyncio
async def test_restore_source_removes_stored_names_and_requests_trigger_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    hidden_dir, _hidden_hardware_dir, _sys_input_dir = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    hidden_dir.mkdir(parents=True)
    (hidden_dir / "event22").write_text("1\n", encoding="utf-8")
    (hidden_dir / "js0").write_text("1\n", encoding="utf-8")
    calls = _fake_jobs(monkeypatch)

    await source_hiding.restore_source_by_kernel_names(["event22", "js0"])

    assert not (hidden_dir / "event22").exists()
    assert not (hidden_dir / "js0").exists()
    assert [call["names"] for call in calls] == [["event22", "js0"]]


@pytest.mark.asyncio
async def test_restore_source_ignores_invalid_names_without_starting_a_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_paths(monkeypatch, tmp_path)
    calls = _fake_jobs(monkeypatch)

    await source_hiding.restore_source_by_kernel_names(["../etc", "mouse0", ""])

    assert calls == []


@pytest.mark.asyncio
async def test_hide_source_returns_written_flags_when_trigger_job_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hidden_dir, _hidden_hardware_dir, _sys_input_dir = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    _fake_jobs(
        monkeypatch,
        error=OSError("systemctl failed: Unit keymasq-hardware@abc.service not found"),
    )

    hidden_names = await source_hiding.hide_source("/dev/input/event22")

    assert hidden_names == ["event22"]
    assert (hidden_dir / "event22").exists()
    assert "udev trigger job failed for event22" in caplog.text
    assert SOURCE_HIDING_JOB_HINT in caplog.text


@pytest.mark.asyncio
async def test_trigger_job_timeout_logs_without_the_install_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure_paths(monkeypatch, tmp_path)
    _fake_jobs(monkeypatch, error=TimeoutError())

    hidden_names = await source_hiding.hide_source("/dev/input/event22")

    assert hidden_names == ["event22"]
    assert "Timed out triggering udev for event22" in caplog.text
    assert SOURCE_HIDING_JOB_HINT not in caplog.text


@pytest.mark.asyncio
async def test_unexpected_trigger_job_error_is_logged_and_best_effort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hidden_dir, _hidden_hardware_dir, _sys_input_dir = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    _fake_jobs(monkeypatch, error=RuntimeError("job bug"))

    hidden_names = await source_hiding.hide_source("/dev/input/event22")

    assert hidden_names == ["event22"]
    assert (hidden_dir / "event22").exists()
    assert "Unexpected failure triggering udev for event22" in caplog.text


@pytest.mark.asyncio
async def test_hide_source_rolls_back_flags_when_cancelled_during_trigger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    hidden_dir, _hidden_hardware_dir, sys_input_dir = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    _add_js_sibling(sys_input_dir, "event22", "js0")
    trigger_calls: list[list[str]] = []

    async def fake_trigger_input_nodes(names: list[str], *, timeout_s: float) -> bool:
        _ = timeout_s
        trigger_calls.append(list(names))
        if len(trigger_calls) == 1:
            raise asyncio.CancelledError()
        return True

    monkeypatch.setattr(source_hiding, "_trigger_input_nodes", fake_trigger_input_nodes)

    with pytest.raises(asyncio.CancelledError):
        await source_hiding.hide_source("/dev/input/event22")

    assert not (hidden_dir / "event22").exists()
    assert not (hidden_dir / "js0").exists()
    assert trigger_calls == [["event22", "js0"], ["event22", "js0"]]


@pytest.mark.asyncio
async def test_hide_source_rolls_back_flags_when_cancelled_during_flag_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    hidden_dir, _hidden_hardware_dir, sys_input_dir = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    _add_js_sibling(sys_input_dir, "event22", "js0")
    write_started = asyncio.Event()
    allow_write = asyncio.Event()
    trigger_calls: list[list[str]] = []

    async def fake_to_thread(func, /, *args, **kwargs):
        write_started.set()
        await allow_write.wait()
        return func(*args, **kwargs)

    async def fake_trigger_input_nodes(names: list[str], *, timeout_s: float) -> bool:
        _ = timeout_s
        trigger_calls.append(list(names))
        return True

    monkeypatch.setattr(source_hiding.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(source_hiding, "_trigger_input_nodes", fake_trigger_input_nodes)

    task = asyncio.create_task(source_hiding.hide_source("/dev/input/event22"))
    await write_started.wait()
    task.cancel()
    allow_write.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not (hidden_dir / "event22").exists()
    assert not (hidden_dir / "js0").exists()
    assert trigger_calls == [["event22", "js0"]]


@pytest.mark.asyncio
async def test_hide_source_rejects_non_event_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    hidden_dir, _hidden_hardware_dir, _sys_input_dir = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    calls = _fake_jobs(monkeypatch)

    hidden_names = await source_hiding.hide_source("/dev/input/js0")

    assert hidden_names == []
    assert not hidden_dir.exists()
    assert calls == []


@pytest.mark.asyncio
async def test_reconcile_all_clears_flags_and_triggers_the_input_subsystem(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    hidden_dir, hidden_hardware_dir, _sys_input_dir = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    hidden_dir.mkdir(parents=True)
    (hidden_dir / "event22").write_text("1\n", encoding="utf-8")
    (hidden_dir / "js0").write_text("1\n", encoding="utf-8")
    hidden_hardware_dir.mkdir(parents=True)
    (hidden_hardware_dir / "045e:02a1").write_text("1\n", encoding="utf-8")
    calls = _fake_jobs(monkeypatch)

    await source_hiding.reconcile_all()

    assert list(hidden_dir.iterdir()) == []
    assert list(hidden_hardware_dir.iterdir()) == []
    assert calls == [
        {"operation": "trigger", "id": "", "timeout": source_hiding.RECONCILE_TIMEOUT_S}
    ]


def test_hardware_flag_name_normalizes_common_hardware_id_forms() -> None:
    assert source_hiding.hardware_flag_name("045E:02A1") == "045e:02a1"
    assert source_hiding.hardware_flag_name("45e:2a1") == "045e:02a1"
    assert source_hiding.hardware_flag_name("keymasq:045e:02a1") == "045e:02a1"
    assert source_hiding.hardware_flag_name("045e:02a1@1") == "045e:02a1"
    assert source_hiding.hardware_flag_name("not-a-hardware-id") is None


@pytest.mark.asyncio
async def test_enable_and_disable_hardware_hotplug_hiding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _hidden_dir, hidden_hardware_dir, _sys_input_dir = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    calls = _fake_jobs(monkeypatch)

    enabled = await source_hiding.enable_hardware_hotplug_hiding("045e:02a1")

    assert enabled is True
    assert (hidden_hardware_dir / "045e:02a1").read_text(encoding="utf-8") == "1\n"
    assert calls == []

    disabled = await source_hiding.disable_hardware_hotplug_hiding("keymasq:045e:02a1@2")

    assert disabled is True
    assert not (hidden_hardware_dir / "045e:02a1").exists()
    assert calls == [
        {"operation": "trigger", "id": "", "timeout": source_hiding.RECONCILE_TIMEOUT_S}
    ]


@pytest.mark.asyncio
async def test_enable_hardware_hotplug_hiding_rejects_invalid_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _hidden_dir, hidden_hardware_dir, _sys_input_dir = _configure_paths(
        monkeypatch,
        tmp_path,
    )

    enabled = await source_hiding.enable_hardware_hotplug_hiding("gamepad")

    assert enabled is False
    assert not hidden_hardware_dir.exists()
