import configparser
import re
from pathlib import Path

import pytest

from keymasq.masking import operations

ROOT = Path(__file__).resolve().parents[2]
PREPARE = "/usr/bin/keymasq-record prepare-removal"


@pytest.fixture
def host(tmp_path, monkeypatch):
    """Fake the host commands prepare_removal() runs, recording each call."""
    systemd = tmp_path / "run/systemd/system"
    systemd.mkdir(parents=True)
    devices = tmp_path / "dev"
    (devices / "input").mkdir(parents=True)
    for name in ("uinput", "input/event3", "input/js0", "hidraw2", "input/mouse0"):
        (devices / name).touch()
    monkeypatch.setattr(operations, "SYSTEMD_RUNTIME", systemd)
    monkeypatch.setattr(operations, "DEVICE_ROOT", devices)
    monkeypatch.setattr(operations.pwd, "getpwnam", lambda name: object())

    class Host:
        calls: list[tuple[str, ...]] = []
        state = "inactive"
        stop_error: OSError | None = None
        recovery_error: OSError | None = None

    async def run_host(*args, **kwargs):
        Host.calls.append(args)
        if args[:2] == ("systemctl", "stop") and Host.stop_error is not None:
            raise Host.stop_error
        if args[:2] == ("systemctl", "show"):
            return Host.state + "\n"
        return ""

    async def recover_hardware():
        Host.calls.append(("recover-hardware",))
        if Host.recovery_error is not None:
            raise Host.recovery_error

    monkeypatch.setattr(operations, "run_host", run_host)
    monkeypatch.setattr(operations, "recover_hardware", recover_hardware)
    Host.calls = []
    Host.devices = devices
    return Host


@pytest.mark.asyncio
async def test_removal_stops_daemon_recovers_then_removes_only_keymasq_grants(host):
    await operations.prepare_removal()

    devices = host.devices
    assert host.calls == [
        ("systemctl", "stop", "keymasqd.service"),
        ("systemctl", "show", "keymasqd.service", "-p", "ActiveState", "--value"),
        ("recover-hardware",),
        ("setfacl", "-x", "u:keymasq", str(devices / "uinput")),
        ("setfacl", "-x", "u:keymasq", str(devices / "input/event3")),
        ("setfacl", "-x", "u:keymasq", str(devices / "input/js0")),
        ("setfacl", "-x", "u:keymasq", str(devices / "hidraw2")),
    ]


@pytest.mark.asyncio
async def test_incomplete_recovery_blocks_removal_and_keeps_grants(host):
    host.recovery_error = OSError("Hardware recovery remains incomplete: 1234: port busy")

    with pytest.raises(operations.RemovalBlockedError) as error:
        await operations.prepare_removal()

    assert "cannot be removed yet" in str(error.value)
    assert "Hardware recovery remains incomplete: 1234: port busy" in str(error.value)
    assert "sudo keymasq-record recover-hardware" in str(error.value)
    assert not [call for call in host.calls if call[0] == "setfacl"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["active", "deactivating", ""])
async def test_daemon_that_did_not_stop_blocks_removal(host, state):
    host.state = state

    with pytest.raises(operations.RemovalBlockedError, match="keymasqd.service is still"):
        await operations.prepare_removal()

    assert ("recover-hardware",) not in host.calls


@pytest.mark.asyncio
async def test_failed_stop_of_an_already_stopped_daemon_does_not_block_removal(host):
    # systemctl reports stop failures that leave nothing running, and it
    # reports success when ExecStopPost recovery fails. The state and the
    # explicit recovery below decide instead.
    host.stop_error = OSError("systemctl failed: Unit keymasqd.service not loaded")
    host.state = "failed"

    await operations.prepare_removal()

    assert ("recover-hardware",) in host.calls


@pytest.mark.asyncio
async def test_removal_without_systemd_has_nothing_to_recover(host, monkeypatch, tmp_path):
    monkeypatch.setattr(operations, "SYSTEMD_RUNTIME", tmp_path / "missing")

    await operations.prepare_removal()

    assert host.calls == []


@pytest.mark.asyncio
async def test_grant_removal_failures_do_not_block_removal(host, monkeypatch):
    async def run_host(*args, **kwargs):
        host.calls.append(args)
        if args[0] == "setfacl":
            raise OSError("setfacl failed: Operation not supported")
        return "inactive\n" if args[:2] == ("systemctl", "show") else ""

    monkeypatch.setattr(operations, "run_host", run_host)

    await operations.prepare_removal()


def test_prepare_removal_requires_root_outside_recording_authorization(monkeypatch):
    monkeypatch.setattr(operations.os, "geteuid", lambda: 1000)
    with pytest.raises(PermissionError, match="require root"):
        operations.main("prepare-removal")
    monkeypatch.setattr(operations.os, "geteuid", lambda: 0)
    monkeypatch.setenv("PKEXEC_UID", "1000")
    with pytest.raises(PermissionError, match="Recording authorization"):
        operations.main("prepare-removal")


def _blocking_calls(script: str) -> list[str]:
    return [line.strip() for line in script.splitlines() if PREPARE in line]


def test_debian_removal_is_refused_when_recovery_is_incomplete():
    prerm = (ROOT / "debian/keymasq.prerm").read_text()
    calls = _blocking_calls(prerm)
    # set -e turns a helper failure into a failed prerm, and dpkg keeps the package.
    assert calls == [PREPARE]
    assert "set -e" in prerm
    assert prerm.index('"$1" = remove') < prerm.index(PREPARE) < prerm.index("#DEBHELPER#")


def test_rpm_erase_is_refused_when_recovery_is_incomplete():
    preremove = (ROOT / "scripts/rpm-preremove.sh").read_text()
    assert _blocking_calls(preremove) == [PREPARE]
    assert "set -e" in preremove
    # Disabling first would leave a kept package with its service disabled.
    assert preremove.index(PREPARE) < preremove.index("systemctl disable")
    assert "--now" not in preremove


def test_pacman_hook_aborts_the_removal_transaction():
    hook = configparser.ConfigParser(allow_no_value=True)
    hook.optionxform = str  # type: ignore[assignment,method-assign]
    hook.read(ROOT / "packaging/pacman/keymasq-prepare-removal.hook")
    assert dict(hook["Trigger"]) == {
        "Operation": "Remove",
        "Type": "Package",
        "Target": "keymasq",
    }
    assert hook["Action"]["When"] == "PreTransaction"
    assert hook["Action"]["Exec"] == PREPARE
    assert "AbortOnFail" in hook["Action"]


@pytest.mark.parametrize(
    "pkgbuild",
    ["packaging/pacman/templates/PKGBUILD.in", "PKGBUILD", "packaging/aur/PKGBUILD"],
)
def test_pacman_packages_install_the_removal_hook(pkgbuild):
    assert re.search(
        r'install -Dm644 "packaging/pacman/keymasq-prepare-removal\.hook" \\\n\s+'
        r'"\$pkgdir/usr/share/libalpm/hooks/keymasq-prepare-removal\.hook"',
        (ROOT / pkgbuild).read_text(),
    )


def test_source_tarball_ships_the_removal_hook():
    tarball = (ROOT / "scripts/build-source-tarball.sh").read_text()
    assert "    packaging/pacman/keymasq-prepare-removal.hook\n" in tarball


@pytest.mark.parametrize(
    "path",
    [
        "debian/keymasq.postrm",
        "scripts/rpm-postremove.sh",
        "packaging/pacman/templates/keymasq.install.in",
        "keymasq.install",
        "packaging/aur/keymasq.install",
    ],
)
def test_native_removal_lets_remaining_udev_policy_recompute_permissions(path):
    script = (ROOT / path).read_text()
    assert "udevadm control --reload-rules" in script
    assert "udevadm trigger --action=change --subsystem-match=input" in script
    assert "udevadm trigger --action=change --sysname-match=uinput" in script
    # Only keymasq's grant is removed. Other ACLs belong to udev and logind.
    assert "setfacl -b" not in script


@pytest.mark.parametrize(
    "path",
    [
        "debian/keymasq.postinst",
        "scripts/rpm-postinstall.sh",
        "packaging/pacman/templates/keymasq.install.in",
        "keymasq.install",
        "packaging/aur/keymasq.install",
    ],
)
def test_native_install_explains_how_to_hand_over_from_the_appimage(path):
    script = (ROOT / path).read_text()
    check = script.index("if [ -e /opt/keymasq/version ]")
    handover = script[check:]
    assert "overrides this package" in handover
    assert handover.index("/opt/keymasq/bin/keymasq --uninstall") < handover.index(
        "sudo systemctl enable --now keymasqd"
    )
    assert "systemctl --user enable --now keymasq-session" in handover
    # systemctl revert would delete the AppImage unit and strand its other files.
    if "systemctl revert" in script:
        assert script.index("systemctl revert") > check
