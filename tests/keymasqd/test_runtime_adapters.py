from keymasq.keymasqd.runtime.adapters import close_device


class _ClosableDevice:
    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


class _FailingCloseDevice:
    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1
        raise OSError("close failed")


def test_close_device_invokes_callable_close() -> None:
    device = _ClosableDevice()

    close_device(device)

    assert device.close_count == 1


def test_close_device_ignores_missing_or_failing_close() -> None:
    failing_device = _FailingCloseDevice()

    close_device(object())
    close_device(failing_device)

    assert failing_device.close_count == 1
