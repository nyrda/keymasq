"""Masking commands never depend on the caller's executable search path."""

from unittest.mock import AsyncMock

import pytest

from keymasq.masking import backend, commands, permissions
from tests.common.test_generic_hardware_masking import generic_usb


@pytest.fixture
def trusted_tools(tmp_path, monkeypatch):
    trusted, caller = tmp_path / "trusted", tmp_path / "caller"
    for directory, output in ((trusted, "trusted"), (caller, "caller")):
        directory.mkdir()
        for name in commands.COMMAND_NAMES:
            executable = directory / name
            executable.write_text(f"#!/bin/sh\nprintf '%s\\n' {output}\n")
            executable.chmod(0o755)
    monkeypatch.setattr(commands, "BUILD_COMMAND_PATHS", {})
    monkeypatch.setattr(commands, "SYSTEM_PATH", str(trusted))
    monkeypatch.setenv("PATH", str(caller))
    return trusted


@pytest.mark.asyncio
@pytest.mark.parametrize("name", commands.COMMAND_NAMES)
async def test_subprocess_ignores_caller_path(trusted_tools, name):
    assert await backend.run_host(name) == "trusted\n"


@pytest.mark.asyncio
async def test_package_path_takes_precedence_over_system_and_caller_paths(
    trusted_tools, tmp_path, monkeypatch
):
    packaged = tmp_path / "package-command"
    packaged.write_text("#!/bin/sh\nprintf 'packaged\\n'\n")
    packaged.chmod(0o755)
    monkeypatch.setattr(commands, "BUILD_COMMAND_PATHS", {"udevadm": str(packaged)})
    assert await backend.run_host("udevadm") == "packaged\n"


@pytest.mark.asyncio
async def test_missing_system_tool_does_not_fall_back_to_caller_path(trusted_tools):
    (trusted_tools / "udevadm").unlink()
    with pytest.raises(FileNotFoundError, match="Required masking command"):
        await backend.run_host("udevadm")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "not_executable", "relative"])
async def test_invalid_package_tool_does_not_fall_back(trusted_tools, tmp_path, monkeypatch, kind):
    packaged = tmp_path / "package-command"
    if kind == "not_executable":
        packaged.touch(mode=0o644)
    monkeypatch.setattr(
        commands,
        "BUILD_COMMAND_PATHS",
        {"udevadm": "udevadm" if kind == "relative" else str(packaged)},
    )
    with pytest.raises((FileNotFoundError, ValueError)):
        await backend.run_host("udevadm")


@pytest.mark.parametrize("name", ["sh", "/usr/bin/udevadm", "./udevadm"])
def test_only_known_command_names_are_accepted(trusted_tools, name):
    with pytest.raises(ValueError, match="Unknown masking command"):
        commands.command_path(name)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [None, "chmod", "setfacl", "udevadm", "getfacl", "systemctl"])
async def test_udev_rules_use_trusted_tools_and_preflight_dependencies(
    trusted_tools, tmp_path, monkeypatch, missing
):
    inventory, attachment, _hid, _driver = generic_usb(tmp_path)
    transaction = backend.LinuxMaskBackend(
        inventory, tmp_path / "run", tmp_path / "rules", tmp_path / "state"
    )
    transaction.prepare_directories()
    capture = AsyncMock()
    monkeypatch.setattr(permissions, "capture", capture)
    if missing:
        (trusted_tools / missing).unlink()
        with pytest.raises(FileNotFoundError, match="Required masking command"):
            await transaction.install_rules(attachment)
        assert not transaction.armed_record.exists()
        assert not transaction.early.exists()
        assert not transaction.late.exists()
        capture.assert_not_awaited()
    else:
        await transaction.install_rules(attachment)
        rules = transaction.late.read_text()
        assert f'RUN+="{trusted_tools}/chmod 0660 ' in rules
        assert f'RUN+="{trusted_tools}/setfacl -b ' in rules
        # With an ACL mask present, chmod only changes the mask entry and a
        # later setfacl -b re-exposes the group entry. Strip ACLs first.
        for line in rules.splitlines():
            assert line.index("setfacl -b") < line.index("chmod 0660"), line
        assert str(tmp_path / "caller") not in rules
