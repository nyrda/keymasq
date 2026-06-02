import evdev


class FakeRecorder:
    def __init__(self) -> None:
        self.is_recording = True
        self.calls: list[tuple[str, evdev.InputEvent]] = []

    def record_event(self, device_type: str, event: evdev.InputEvent) -> None:
        self.calls.append((device_type, event))
