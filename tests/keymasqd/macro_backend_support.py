from typing import cast

import evdev

import keymasq.keymasqd.device_manager as dm
from keymasq.common.models import (
    DEFAULT_MACRO_LOOP_STOP_BEHAVIOR,
    normalize_macro_loop_stop_behavior,
)
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.runtime import macros as mdm


class FakeRecorder:
    def __init__(self) -> None:
        self.is_recording = True
        self.calls: list[tuple[str, evdev.InputEvent]] = []

    def record_event(self, device_type: str, event: evdev.InputEvent) -> None:
        self.calls.append((device_type, event))


async def play_macro_task_helper(manager: DeviceManager, **kwargs: object) -> None:
    instance_id = int(kwargs["instance_id"])
    loop_stop_behavior = normalize_macro_loop_stop_behavior(
        kwargs.get("loop_stop_behavior", DEFAULT_MACRO_LOOP_STOP_BEHAVIOR)
    )
    manager.macro_state.instance_meta.setdefault(instance_id, {})[
        "loop_stop_behavior"
    ] = loop_stop_behavior
    await mdm.play_macro_task(
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
        deps=dm._macro_runtime_deps(),
        macro_event_source=cast(
            mdm.MacroEventSource | None,
            kwargs.get("macro_event_source"),
        ),
    )
