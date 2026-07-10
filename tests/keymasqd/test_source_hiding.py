import asyncio
from pathlib import Path
from typing import Any

import pytest

from keymasq.keymasqd.permission_hints import CAPABILITY_PERMISSION_HINT
from keymasq.keymasqd.runtime import source_hiding
from tests.async_fakes import FakeProcess as _FakeProcess


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


def _fake_udevadm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stderr: bytes = b"",
) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(source_hiding, "resolve_udevadm_path", lambda: "/usr/bin/udevadm")

    async def fake_create_subprocess_exec(*args: Any, **_kwargs: Any) -> _FakeProcess:
        calls.append(args)
        return _FakeProcess(returncode=returncode, stderr=stderr)

    monkeypatch.setattr(
        source_hiding.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    return calls


@pytest.mark.asyncio
async def test_hide_source_flags_event_and_js_sibling_and_triggers_udev(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    hidden_dir, _hidden_hardware_dir, sys_input_dir = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    _add_js_sibling(sys_input_dir, "event22", "js0")
    calls = _fake_udevadm(monkeypatch)

    hidden_names = await source_hiding.hide_source("/dev/input/event22")

    assert hidden_names == ["event22", "js0"]
    assert (hidden_dir / "event22").read_text(encoding="utf-8") == "1\n"
    assert (hidden_dir / "js0").read_text(encoding="utf-8") == "1\n"
    assert calls == [
        (
            "/usr/bin/udevadm",
            "trigger",
            "--subsystem-match=input",
            "--action=change",
            "--sysname-match=event22",
            "--settle",
        ),
        (
            "/usr/bin/udevadm",
            "trigger",
            "--subsystem-match=input",
            "--action=change",
            "--sysname-match=js0",
            "--settle",
        ),
    ]


@pytest.mark.asyncio
async def test_restore_source_removes_stored_names_and_triggers_udev(
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
    calls = _fake_udevadm(monkeypatch)

    await source_hiding.restore_source_by_kernel_names(["event22", "js0"])

    assert not (hidden_dir / "event22").exists()
    assert not (hidden_dir / "js0").exists()
    assert [call[4] for call in calls] == [
        "--sysname-match=event22",
        "--sysname-match=js0",
    ]


@pytest.mark.asyncio
async def test_hide_source_returns_written_flags_when_udev_trigger_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hidden_dir, _hidden_hardware_dir, _sys_input_dir = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    _fake_udevadm(monkeypatch, returncode=1, stderr=b"permission denied")

    hidden_names = await source_hiding.hide_source("/dev/input/event22")

    assert hidden_names == ["event22"]
    assert (hidden_dir / "event22").exists()
    assert "udevadm trigger failed" in caplog.text


@pytest.mark.asyncio
async def test_udev_permission_failure_logs_capability_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure_paths(monkeypatch, tmp_path)
    _fake_udevadm(
        monkeypatch,
        returncode=1,
        stderr=b"Failed to write 'change' to uevent: Permission denied",
    )

    await source_hiding.hide_source("/dev/input/event22")

    assert CAPABILITY_PERMISSION_HINT in caplog.text


@pytest.mark.asyncio
async def test_udev_non_permission_failure_omits_capability_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure_paths(monkeypatch, tmp_path)
    _fake_udevadm(monkeypatch, returncode=1, stderr=b"device is busy")

    await source_hiding.hide_source("/dev/input/event22")

    assert "udevadm trigger failed" in caplog.text
    assert CAPABILITY_PERMISSION_HINT not in caplog.text


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
    trigger_calls: list[str] = []

    async def fake_trigger_input_node(name: str, *, timeout_s: float) -> bool:
        _ = timeout_s
        trigger_calls.append(name)
        if trigger_calls == ["event22"]:
            raise asyncio.CancelledError()
        return True

    monkeypatch.setattr(
        source_hiding,
        "_trigger_input_node",
        fake_trigger_input_node,
    )

    with pytest.raises(asyncio.CancelledError):
        await source_hiding.hide_source("/dev/input/event22")

    assert not (hidden_dir / "event22").exists()
    assert not (hidden_dir / "js0").exists()
    assert trigger_calls == ["event22", "event22", "js0"]


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
    trigger_calls: list[str] = []

    async def fake_to_thread(func, /, *args, **kwargs):
        write_started.set()
        await allow_write.wait()
        return func(*args, **kwargs)

    async def fake_trigger_input_node(name: str, *, timeout_s: float) -> bool:
        _ = timeout_s
        trigger_calls.append(name)
        return True

    monkeypatch.setattr(source_hiding.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        source_hiding,
        "_trigger_input_node",
        fake_trigger_input_node,
    )

    task = asyncio.create_task(source_hiding.hide_source("/dev/input/event22"))
    await write_started.wait()
    task.cancel()
    allow_write.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not (hidden_dir / "event22").exists()
    assert not (hidden_dir / "js0").exists()
    assert trigger_calls == ["event22", "js0"]


@pytest.mark.asyncio
async def test_hide_source_is_best_effort_when_udevadm_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hidden_dir, _hidden_hardware_dir, _sys_input_dir = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    monkeypatch.setattr(source_hiding, "resolve_udevadm_path", lambda: None)

    hidden_names = await source_hiding.hide_source("/dev/input/event22")

    assert hidden_names == ["event22"]
    assert (hidden_dir / "event22").exists()
    assert "udevadm not found" in caplog.text


@pytest.mark.asyncio
async def test_hide_source_rejects_non_event_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    hidden_dir, _hidden_hardware_dir, _sys_input_dir = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    calls = _fake_udevadm(monkeypatch)

    hidden_names = await source_hiding.hide_source("/dev/input/js0")

    assert hidden_names == []
    assert not hidden_dir.exists()
    assert calls == []


@pytest.mark.asyncio
async def test_reconcile_all_clears_flags_and_triggers_input(
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
    calls = _fake_udevadm(monkeypatch)

    await source_hiding.reconcile_all()

    assert list(hidden_dir.iterdir()) == []
    assert list(hidden_hardware_dir.iterdir()) == []
    assert calls == [
        (
            "/usr/bin/udevadm",
            "trigger",
            "--subsystem-match=input",
            "--action=change",
            "--settle",
        )
    ]


@pytest.mark.asyncio
async def test_udevadm_runs_with_host_tool_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(source_hiding, "resolve_udevadm_path", lambda: "/usr/bin/udevadm")
    monkeypatch.setenv("APPDIR", "/tmp/.mount_Keymasq")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/.mount_Keymasq/lib")
    monkeypatch.setenv("PYTHONPATH", "/tmp/.mount_Keymasq/lib/python3.12")
    monkeypatch.setenv("LANG", "C.UTF-8")
    captured_env: dict[str, str] | None = None

    async def fake_create_subprocess_exec(
        *_args: Any,
        **kwargs: Any,
    ) -> _FakeProcess:
        nonlocal captured_env
        captured_env = kwargs.get("env")
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(
        source_hiding.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    result = await source_hiding._run_udevadm(  # pyright: ignore[reportPrivateUsage]
        ["trigger", "--subsystem-match=input"],
        timeout_s=0.01,
        context="test",
    )

    assert result is True
    assert captured_env is not None
    assert captured_env["LANG"] == "C.UTF-8"
    assert captured_env["PATH"] == "/usr/sbin:/usr/bin:/sbin:/bin"
    assert "APPDIR" not in captured_env
    assert "LD_LIBRARY_PATH" not in captured_env
    assert "PYTHONPATH" not in captured_env


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
    calls = _fake_udevadm(monkeypatch)

    enabled = await source_hiding.enable_hardware_hotplug_hiding("045e:02a1")

    assert enabled is True
    assert (hidden_hardware_dir / "045e:02a1").read_text(encoding="utf-8") == "1\n"

    disabled = await source_hiding.disable_hardware_hotplug_hiding("keymasq:045e:02a1@2")

    assert disabled is True
    assert not (hidden_hardware_dir / "045e:02a1").exists()
    assert calls == [
        (
            "/usr/bin/udevadm",
            "trigger",
            "--subsystem-match=input",
            "--action=change",
            "--settle",
        )
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


@pytest.mark.asyncio
async def test_udevadm_timeout_terminates_process(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_process = _FakeProcess()
    monkeypatch.setattr(source_hiding, "resolve_udevadm_path", lambda: "/usr/bin/udevadm")

    async def fake_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return fake_process

    async def fake_wait_for(awaitable: Any, **_kwargs: Any) -> None:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise TimeoutError

    monkeypatch.setattr(
        source_hiding.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(source_hiding.asyncio, "wait_for", fake_wait_for)

    result = await source_hiding._trigger_input_node(  # pyright: ignore[reportPrivateUsage]
        "event22",
        timeout_s=0.01,
    )

    assert result is False
    assert fake_process.terminated is True
    assert "Timed out triggering udev" in caplog.text
    await asyncio.sleep(0)
