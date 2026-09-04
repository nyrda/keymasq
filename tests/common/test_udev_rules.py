from pathlib import Path


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
