from typing import cast

import evdev

from keymasq.keymasqd import device_manager
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.runtime.macro import scheduler
from keymasq.keymasqd.runtime.macro.state import MacroEventSource


class FakeRecorder:
    def __init__(self) -> None:
        self.is_recording = True
        self.calls: list[tuple[str, evdev.InputEvent]] = []

    def record_event(self, device_type: str, event: evdev.InputEvent) -> None:
        self.calls.append((device_type, event))


async def play_macro_task_helper(manager: DeviceManager, **kwargs: object) -> None:
    instance_id = int(kwargs["instance_id"])
    await scheduler.play_macro_task(
        manager,
        instance_id=instance_id,
        macro_events=cast(list[dict[str, object]], kwargs["macro_events"]),
        macro_name=str(kwargs["macro_name"]),
        replay_mouse_movement=bool(kwargs["replay_mouse_movement"]),
        replay_mouse_clicks=bool(kwargs["replay_mouse_clicks"]),
        speed=float(kwargs["speed"]),
        loop_mode=str(kwargs["loop_mode"]),
        loop_count=int(kwargs["loop_count"]),
        move_to_start=bool(kwargs["move_to_start"]),
        start_x=int(kwargs["start_x"]),
        start_y=int(kwargs["start_y"]),
        block_mouse_movement=bool(kwargs["block_mouse_movement"]),
        deps=device_manager._macro_runtime_deps(),
        macro_event_source=cast(
            MacroEventSource | None,
            kwargs.get("macro_event_source"),
        ),
    )
