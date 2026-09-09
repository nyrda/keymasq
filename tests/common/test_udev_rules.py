from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def test_native_hidraw_acl_is_read_only_and_independent_of_driver_models() -> None:
    lines = (ROOT / "udev/91-keymasq-acl.rules").read_text().splitlines()
    hidraw_rules = [line for line in lines if 'SUBSYSTEM=="hidraw"' in line]
    assert len(hidraw_rules) == 1
    rule = hidraw_rules[0]
    assert 'ACTION=="add|change"' in rule
    assert "ATTRS{" not in rule
    assert 'KERNEL=="hidraw*"' in rule
    assert 'RUN+="/usr/bin/setfacl -m u:keymasq:r /dev/%k"' in rule
    assert "MODE=" not in rule
    assert "GROUP=" not in rule


@pytest.mark.parametrize(
    "path",
    [
        "systemd/keymasqd.service",
        "packaging/appimage/assets/keymasqd.service",
        "packaging/appimage/runtime/keymasq-appimage-runtime.sh",
        "flake.nix",
        "scripts/install-systemd.sh",
        "debian/keymasq.postinst",
        "scripts/rpm-postinstall.sh",
        "keymasq.install",
        "packaging/aur/keymasq.install",
        "packaging/pacman/templates/keymasq.install.in",
    ],
)
def test_install_and_startup_apply_native_acls_to_connected_devices(path: str) -> None:
    assert "udevadm trigger --subsystem-match=hidraw --action=change --settle" in (
        ROOT / path
    ).read_text()


def test_hardware_hotplug_udev_rule_only_hides_joystick_event_nodes() -> None:
    rule_path = Path(__file__).parents[2] / "udev" / "99-keymasq-hide-grabbed.rules"
    lines = rule_path.read_text(encoding="utf-8").splitlines()

    event_hardware_lines = [
        line
        for line in lines
        if 'KERNEL=="event*"' in line and "/run/keymasq/hidden-hardware/" in line
    ]
    js_hardware_lines = [
        line
        for line in lines
        if 'KERNEL=="js*"' in line and "/run/keymasq/hidden-hardware/" in line
    ]

    assert len(event_hardware_lines) == 1
    assert len(js_hardware_lines) == 1
    assert 'ENV{ID_INPUT_JOYSTICK}=="?*"' in event_hardware_lines[0]
    assert event_hardware_lines[0].index(
        'ENV{ID_INPUT_JOYSTICK}=="?*"',
    ) < event_hardware_lines[0].index('ENV{ID_INPUT_JOYSTICK}=""')
