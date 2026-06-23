from keymasq.keymasqd.permission_hints import (
    UINPUT_PERMISSION_HINT,
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
