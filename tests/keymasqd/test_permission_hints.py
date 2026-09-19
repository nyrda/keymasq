from keymasq.keymasqd.permission_hints import (
    SOURCE_HIDING_JOB_HINT,
    UINPUT_PERMISSION_HINT,
    source_hiding_job_message,
    uinput_permission_message,
)


def test_uinput_permission_message_appends_hint_when_error_mentions_devnode() -> None:
    message = uinput_permission_message(
        'Failed to create keyboard uinput device: "/dev/uinput" cannot be opened for writing'
    )

    assert UINPUT_PERMISSION_HINT in message


def test_uinput_permission_message_does_not_duplicate_existing_hint() -> None:
    message = uinput_permission_message(
        f"Failed to create keyboard uinput device. {UINPUT_PERMISSION_HINT}"
    )

    assert message.count(UINPUT_PERMISSION_HINT) == 1


def test_source_hiding_job_message_appends_hint() -> None:
    message = source_hiding_job_message(
        "udev trigger job failed for event5: systemctl failed: "
        "Unit keymasq-hardware@abc.service not found"
    )

    assert SOURCE_HIDING_JOB_HINT in message


def test_source_hiding_job_message_does_not_duplicate_existing_hint() -> None:
    message = source_hiding_job_message(
        f"udev trigger job failed for event5. {SOURCE_HIDING_JOB_HINT}"
    )

    assert message.count(SOURCE_HIDING_JOB_HINT) == 1


def test_source_hiding_hint_is_distinct_from_device_hints() -> None:
    assert SOURCE_HIDING_JOB_HINT != UINPUT_PERMISSION_HINT
    assert "keymasq-hardware@.service" in SOURCE_HIDING_JOB_HINT
    assert "CAP_DAC_OVERRIDE" not in SOURCE_HIDING_JOB_HINT
