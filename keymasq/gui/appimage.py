from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from keymasq.common.subprocess_env import host_subprocess_environment

INSTALLED_COMMAND = "/opt/keymasq/bin/keymasq"
UPDATE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
PKEXEC_DISMISSED = 126
PKEXEC_FAILED = 127

_RELEASES_URL = "https://github.com/nyrda/keymasq/releases"
_VERSION_RE = re.compile(r"[0-9A-Za-z.+!-]{1,64}")
_STEP_PREFIX = "==> "
_ERROR_PREFIX = "error: "

Operation = Literal["install", "setup-user", "update", "repair", "uninstall"]

_OPERATION_ARGUMENTS: dict[Operation, tuple[str, ...]] = {
    "install": ("--install",),
    "setup-user": ("--setup-user",),
    "update": ("--self-update",),
    "repair": ("--install",),
    "uninstall": ("--uninstall",),
}


def operation_arguments(operation: Operation) -> tuple[str, ...]:
    return _OPERATION_ARGUMENTS[operation]


@dataclass(frozen=True)
class AppImageRuntime:
    appdir: str
    launcher: str
    appimage: str | None

    def command(self, *arguments: str) -> list[str]:
        return [self.launcher, *arguments]

    def terminal_command(self, operation: Operation) -> str:
        program = self.appimage if self.appimage and operation == "install" else "keymasq"
        return shlex.join([program, *_OPERATION_ARGUMENTS[operation]])

    def environment(self, env: Mapping[str, str] | None = None) -> dict[str, str]:
        clean_env = host_subprocess_environment(env)
        # The launcher picks its command from ARGV0 before it looks at the arguments.
        clean_env.pop("ARGV0", None)
        clean_env["APPDIR"] = self.appdir
        if self.appimage:
            clean_env["APPIMAGE"] = self.appimage
        return clean_env


def detect_runtime(env: Mapping[str, str] | None = None) -> AppImageRuntime | None:
    process_env = os.environ if env is None else env
    appdir = process_env.get("APPDIR", "")
    if not appdir:
        return None
    launcher = os.path.join(appdir, "bin", "keymasq")
    # A launcher without this marker hands the commands used here to the CLI, which rejects them.
    marker = os.path.join(appdir, "share", "keymasq", "appimage", "gui-integration-version")
    if not os.path.isfile(launcher) or not os.path.isfile(marker):
        return None
    appimage = process_env.get("APPIMAGE") or None
    if appimage is not None and not os.path.isfile(appimage):
        appimage = None
    return AppImageRuntime(appdir=appdir, launcher=launcher, appimage=appimage)


def parse_fields(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.replace("_", "").isalnum():
            fields[key] = value.strip()
    return fields


def _tri_state(value: str) -> bool | None:
    if value == "yes":
        return True
    if value == "no":
        return False
    return None


@dataclass(frozen=True)
class InstallStatus:
    installed: bool
    installed_version: str
    loose_appimage: bool
    installed_runtime: bool
    native_package: bool
    systemd: bool
    user: str
    user_setup: bool
    daemon_active: bool | None
    session_active: bool | None
    password_missing: bool
    missing_install_tools: tuple[str, ...]
    missing_update_tools: tuple[str, ...]
    architecture: str


def parse_install_status(output: str) -> InstallStatus | None:
    fields = parse_fields(output)
    if "installed" not in fields:
        return None
    return InstallStatus(
        installed=fields["installed"] == "yes",
        installed_version=fields.get("installed_version", ""),
        loose_appimage=fields.get("launch") == "appimage",
        installed_runtime=fields.get("launch") == "installed",
        native_package=fields.get("native_package") == "yes",
        systemd=fields.get("service_manager") == "systemd",
        user=fields.get("user", ""),
        user_setup=fields.get("user_setup") == "yes",
        daemon_active=_tri_state(fields.get("daemon_active", "")),
        session_active=_tri_state(fields.get("session_active", "")),
        password_missing=fields.get("password") == "missing",
        missing_install_tools=tuple(fields.get("missing_install_tools", "").split()),
        missing_update_tools=tuple(fields.get("missing_update_tools", "").split()),
        architecture=fields.get("architecture", ""),
    )


class SetupNeed(Enum):
    NONE = "none"
    INSTALL = "install"
    VERSION_MISMATCH = "version-mismatch"
    SETUP_USER = "setup-user"
    NATIVE_PACKAGE = "native-package"


def setup_need(status: InstallStatus, gui_version: str) -> SetupNeed:
    if not status.installed:
        return SetupNeed.NATIVE_PACKAGE if status.native_package else SetupNeed.INSTALL
    if status.loose_appimage and status.installed_version != gui_version:
        return SetupNeed.VERSION_MISMATCH
    if not status.user_setup:
        return SetupNeed.SETUP_USER
    return SetupNeed.NONE


_ORDERED_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+)|\.dev(\d+))?")
_STAGE_RANK = {"dev": 0, "a": 1, "b": 2, "rc": 3, "final": 4}


def _version_key(version: str) -> tuple[tuple[int, ...], int, int] | None:
    match = _ORDERED_VERSION_RE.fullmatch(version)
    if match is None:
        return None
    release_raw, stage, stage_number, dev_number = match.groups()
    release = [int(part) for part in release_raw.split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    if dev_number is not None:
        return tuple(release), _STAGE_RANK["dev"], int(dev_number)
    if stage is not None:
        return tuple(release), _STAGE_RANK[stage], int(stage_number)
    return tuple(release), _STAGE_RANK["final"], 0


def compare_versions(current: str, candidate: str) -> int | None:
    current_key = _version_key(current)
    candidate_key = _version_key(candidate)
    if current_key is None or candidate_key is None:
        return None
    return (candidate_key > current_key) - (candidate_key < current_key)


def updates_supported(status: InstallStatus) -> bool:
    return status.installed and not status.loose_appimage and status.architecture == "x86_64"


@dataclass(frozen=True)
class UpdateCheck:
    installed_version: str
    available_version: str
    update_available: bool


def parse_update_check(output: str) -> UpdateCheck | None:
    fields = parse_fields(output)
    installed_version = fields.get("installed_version", "")
    available_version = fields.get("available_version", "")
    if not _VERSION_RE.fullmatch(installed_version) or not _VERSION_RE.fullmatch(available_version):
        return None
    return UpdateCheck(
        installed_version=installed_version,
        available_version=available_version,
        update_available=fields.get("update_available") == "yes",
    )


def update_check_due(last_check: float, now: float) -> bool:
    return last_check > now or now - last_check >= UPDATE_CHECK_INTERVAL_SECONDS


def release_notes_url(version: str) -> str:
    if not _VERSION_RE.fullmatch(version) or "dev" in version:
        return _RELEASES_URL
    return f"{_RELEASES_URL}/tag/v{version}"


def step_from_line(line: str) -> str | None:
    if line.startswith(_STEP_PREFIX):
        return line.removeprefix(_STEP_PREFIX).strip()
    return None


def failure_message(exit_code: int, output_lines: Sequence[str], terminal_command: str) -> str:
    if exit_code == PKEXEC_DISMISSED:
        return "The password prompt was cancelled."
    if exit_code == PKEXEC_FAILED:
        return (
            "Keymasq could not get administrator rights. This needs a password for your "
            "user account and a desktop password prompt, which remote and Game Mode "
            f"sessions do not have. Run this in a terminal instead:\n{terminal_command}"
        )
    for line in reversed(output_lines):
        if line.startswith(_ERROR_PREFIX):
            message = line.removeprefix(_ERROR_PREFIX).strip()
            if message:
                return message[0].upper() + message[1:]
    return f"The command failed with exit code {exit_code}."
