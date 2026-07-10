from keymasq.keymasqd.permission_hints import (
    CAPABILITY_PERMISSION_HINT,
    UINPUT_PERMISSION_HINT,
    capability_permission_message,
    is_capability_permission_failure,
    uinput_permission_message,
)


def test_uinput_permission_message_appends_hint_when_error_mentions_devnode() -> None:
    message = uinput_permission_message(
        'Failed to create keyboard uinput device: "/dev/uinput" '
        "cannot be opened for writing"
    )

    assert UINPUT_PERMISSION_HINT in message


def test_uinput_permission_message_does_not_duplicate_existing_hint() -> None:
    message = uinput_permission_message(
        f"Failed to create keyboard uinput device. {UINPUT_PERMISSION_HINT}"
    )

    assert message.count(UINPUT_PERMISSION_HINT) == 1


def test_capability_permission_message_appends_hint() -> None:
    message = capability_permission_message(
        "udevadm trigger failed for event5: returncode=1 "
        "stderr=Failed to write 'change' to uevent: Permission denied"
    )

    assert CAPABILITY_PERMISSION_HINT in message


def test_capability_permission_message_does_not_duplicate_existing_hint() -> None:
    message = capability_permission_message(
        f"udevadm trigger failed for event5. {CAPABILITY_PERMISSION_HINT}"
    )

    assert message.count(CAPABILITY_PERMISSION_HINT) == 1


def test_capability_hint_is_distinct_from_device_hints() -> None:
    assert CAPABILITY_PERMISSION_HINT != UINPUT_PERMISSION_HINT
    assert "CAP_DAC_OVERRIDE" in CAPABILITY_PERMISSION_HINT


def test_is_capability_permission_failure_matches_permission_markers() -> None:
    assert is_capability_permission_failure("Permission denied")
    assert is_capability_permission_failure("Operation not permitted")
    assert not is_capability_permission_failure("No such file or directory")
    assert not is_capability_permission_failure("")
