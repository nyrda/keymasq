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


def test_uinput_group_access_applies_to_the_static_node() -> None:
    lines = (ROOT / "udev/91-keymasq-acl.rules").read_text().splitlines()
    uinput_rules = [line for line in lines if 'KERNEL=="uinput"' in line]
    assert len(uinput_rules) == 1
    rule = uinput_rules[0]
    # udevd applies only the GROUP and MODE tokens that precede static_node.
    static_node = rule.index('OPTIONS+="static_node=uinput"')
    assert rule.index('GROUP="input"') < static_node
    assert rule.index('MODE="0660"') < static_node


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
    content = (ROOT / path).read_text()
    assert "udevadm trigger --subsystem-match=hidraw --action=change" in content
    assert "udevadm settle --timeout=30" in content
    assert "udevadm trigger --subsystem-match=hidraw --action=change --settle" not in content


@pytest.mark.parametrize(
    "path,settle",
    [
        ("systemd/keymasqd.service", "ExecStartPre=-+/usr/bin/udevadm settle --timeout=30"),
        (
            "packaging/appimage/assets/keymasqd.service",
            "ExecStartPre=-+/usr/bin/udevadm settle --timeout=30",
        ),
        ("scripts/install-systemd.sh", "ExecStartPre=-+/usr/bin/udevadm settle --timeout=30"),
        ("flake.nix", '"-+${pkgs.systemd}/bin/udevadm settle --timeout=30"'),
    ],
)
def test_startup_settle_timeout_does_not_fail_the_daemon(path: str, settle: str) -> None:
    # settle waits for the whole udev queue. A busy boot queue must not block
    # remapping and hardware recovery just to wait for hidraw ACLs.
    assert settle in (ROOT / path).read_text()


@pytest.mark.parametrize(
    "path",
    [
        "debian/keymasq.postinst",
        "scripts/rpm-postinstall.sh",
        "packaging/pacman/templates/keymasq.install.in",
        "keymasq.install",
        "packaging/aur/keymasq.install",
        "scripts/install-systemd.sh",
        "packaging/appimage/runtime/keymasq-appimage-runtime.sh",
    ],
)
def test_install_hooks_do_not_replay_add_rules(path: str) -> None:
    triggers = [
        line for line in (ROOT / path).read_text().splitlines() if "udevadm trigger" in line
    ]
    assert triggers
    assert not [line for line in triggers if "--action=add" in line]


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
