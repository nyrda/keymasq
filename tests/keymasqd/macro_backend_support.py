# pyright: reportUnusedImport=false, reportUnusedFunction=false, reportUnusedClass=false
# ruff: noqa: F401, I001
import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import evdev
import pytest

import keymasq.common.paths as paths
import keymasq.keymasqd.device_manager as dm
import keymasq.keymasqd.recording as recording_module
from keymasq.common.ipc import CommandType
from keymasq.common.models import (
    ActionType,
    DeviceProfileLayer,
    DeviceType,
    MappingAction,
    ProfileConfig,
)
from keymasq.keymasqd.combo_engine import ComboDecision
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.recording import RecordingManager
from keymasq.keymasqd.runtime import grabbed_device as gdm
from keymasq.keymasqd.runtime import grabbed_device_events as gde
from keymasq.keymasqd.runtime import macros as mdm
from keymasq.keymasqd.runtime.grabbed_device import GrabbedDevice
from keymasq.session.profiles import ProfileManager


class _FakeRecorder:
    def __init__(self) -> None:
        self.is_recording = True
        self.calls: list[tuple[str, evdev.InputEvent]] = []

    def record_event(self, device_type: str, event: evdev.InputEvent) -> None:
        self.calls.append((device_type, event))


async def _play_macro_task(manager: DeviceManager, **kwargs: object) -> None:
    await mdm.play_macro_task(
        manager,
        instance_id=int(kwargs["instance_id"]),
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


async def _process_grabbed_event(device: GrabbedDevice, event: evdev.InputEvent) -> None:
    await gde.process_event(
        device,
        event,
        deps=gde.build_event_processing_deps(log=gdm.log),
    )

__all__ = [
    'asyncio',
    'SimpleNamespace',
    'cast',
    'AsyncMock',
    'MagicMock',
    'evdev',
    'pytest',
    'paths',
    'dm',
    'recording_module',
    'CommandType',
    'ActionType',
    'DeviceProfileLayer',
    'DeviceType',
    'MappingAction',
    'ProfileConfig',
    'ComboDecision',
    'DeviceManager',
    'RecordingManager',
    'gdm',
    'gde',
    'mdm',
    'GrabbedDevice',
    'ProfileManager',
    '_FakeRecorder',
    '_play_macro_task',
    '_process_grabbed_event',
]
