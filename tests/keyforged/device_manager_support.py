# pyright: reportUnusedImport=false, reportUnusedFunction=false, reportUnusedClass=false
# ruff: noqa: F401, I001
import asyncio
import contextlib
import errno
import logging
import os
from collections import deque
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import evdev
import pytest

from keyforge.common.ipc import CommandType
from keyforge.common.models import ActionType, DeviceType, MappingAction, SuperkeyMode
from keyforge.keyforged import device_manager as dm
from keyforge.keyforged.combo_engine import ComboDecision, ComboInputEvent
from keyforge.keyforged.device_manager import DesiredGrabConfig, DeviceManager
from keyforge.keyforged.runtime import actions as adm
from keyforge.keyforged.runtime import combos as cdm
from keyforge.keyforged.runtime import grab_lifecycle as ldm
from keyforge.keyforged.runtime import grabbed_device as gdm
from keyforge.keyforged.runtime import grabbed_device_actions as gda
from keyforge.keyforged.runtime import grabbed_device_events as gde
from keyforge.keyforged.runtime import grabbed_device_grab as gdg
from keyforge.keyforged.runtime import grabbed_device_outputs as gdo
from keyforge.keyforged.runtime import grabbed_device_repeat as gdr
from keyforge.keyforged.runtime import grabbed_device_types as gdt
from keyforge.keyforged.runtime import macros as mdm
from keyforge.keyforged.runtime import topology as tdm
from keyforge.keyforged.runtime.grabbed_device import GrabbedDevice
from keyforge.keyforged.superkey_state import SuperkeyActionData, SuperkeyConfig



class _FakeUInput:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.writes: list[tuple[int, int, int]] = []

    def write(self, event_type: int, code: int, value: int) -> None:
        self.writes.append((int(event_type), int(code), int(value)))

    def syn(self) -> None:
        return

    def close(self) -> None:
        return


def _make_grabbed_device(
    monkeypatch: pytest.MonkeyPatch,
    **kwargs,
) -> GrabbedDevice:
    monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
    monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")
    button_map = kwargs.pop("button_map", {})
    button_codes = kwargs.pop("button_codes", None)
    button_values = kwargs.pop("button_values", None)
    keyboard_uinput = kwargs.pop("keyboard_uinput", _FakeUInput())
    mouse_uinput = kwargs.pop("mouse_uinput", _FakeUInput())
    gamepad_uinput = kwargs.pop("gamepad_uinput", _FakeUInput())
    return GrabbedDevice(
        path="/dev/input/event-test",
        hardware_id="1234:5678",
        button_map=button_map,
        button_codes=button_codes,
        button_values=button_values,
        mapping_getter=lambda: {},
        event_callback=AsyncMock(return_value=None),
        keyboard_uinput=keyboard_uinput,  # type: ignore[arg-type]
        mouse_uinput=mouse_uinput,  # type: ignore[arg-type]
        gamepad_uinput=gamepad_uinput,  # type: ignore[arg-type]
        **kwargs,
    )


async def _runtime_on_device_event(
    manager: DeviceManager,
    hardware_id: str,
    evdev_path: str,
    event_type: int,
    event_code: int,
    event_value: int,
    stable_path: str | None = None,
    source: str | None = None,
):
    return await cdm.on_device_event(
        manager,
        hardware_id,
        evdev_path,
        event_type,
        event_code,
        event_value,
        stable_path,
        source,
        resolve_stable_path_fn=dm.resolve_stable_path,
        get_interface_id_fn=dm.get_interface_id,
        combo_binding_cls=dm.RuntimeComboBinding,
        combo_input_event_cls=ComboInputEvent,
        int_value_fn=dm._int_value,
        str_value_fn=dm._str_value,
        time_mod=dm.time,
        action_type_enum=dm.ActionType,
        mapping_action_cls=dm.MappingAction,
        emit_mouse_move_fn=dm._combo_emit_mouse_move_fn(),
        get_trigger_axis_fn=dm.get_trigger_axis,
        resolve_code_fn=dm.resolve_output_code,
        fire_and_observe_fn=dm._fire_and_observe,
        command_type=dm.CommandType,
        asyncio_mod=dm._combo_asyncio_runtime(),
        contextlib_mod=dm.contextlib,
        evdev_mod=dm._combo_evdev_runtime(),
        uinput_writer=dm._combo_uinput_writer(),
    )


async def _runtime_clear_combo_runtime(manager: DeviceManager) -> None:
    await cdm.clear_combo_runtime(
        manager,
        asyncio_mod=dm._combo_asyncio_runtime(),
        contextlib_mod=dm.contextlib,
        mapping_action_cls=dm.MappingAction,
        evdev_mod=dm._combo_evdev_runtime(),
        uinput_writer=dm._combo_uinput_writer(),
        emit_mouse_move_fn=dm._combo_emit_mouse_move_fn(),
        get_trigger_axis_fn=dm.get_trigger_axis,
        resolve_code_fn=dm.resolve_output_code,
        fire_and_observe_fn=dm._fire_and_observe,
        command_type=dm.CommandType,
        action_type_enum=dm.ActionType,
        time_mod=dm.time,
    )


async def _runtime_clear_combo_scope(
    manager: DeviceManager, hardware_id: str, source: str | None = None
) -> None:
    await cdm.clear_combo_runtime_for_binding_scope(
        manager,
        hardware_id,
        source,
        asyncio_mod=dm._combo_asyncio_runtime(),
        contextlib_mod=dm.contextlib,
        mapping_action_cls=dm.MappingAction,
        evdev_mod=dm._combo_evdev_runtime(),
        uinput_writer=dm._combo_uinput_writer(),
        emit_mouse_move_fn=dm._combo_emit_mouse_move_fn(),
        get_trigger_axis_fn=dm.get_trigger_axis,
        resolve_code_fn=dm.resolve_output_code,
        fire_and_observe_fn=dm._fire_and_observe,
        command_type=dm.CommandType,
        action_type_enum=dm.ActionType,
        time_mod=dm.time,
    )


def _runtime_refresh_combo_watchdog(manager: DeviceManager) -> None:
    cdm.refresh_combo_timeout_watchdog(
        manager,
        asyncio_mod=dm._combo_asyncio_runtime(),
        time_mod=dm.time,
        action_type_enum=dm.ActionType,
        mapping_action_cls=dm.MappingAction,
        emit_mouse_move_fn=dm._combo_emit_mouse_move_fn(),
        get_trigger_axis_fn=dm.get_trigger_axis,
        resolve_code_fn=dm.resolve_output_code,
        fire_and_observe_fn=dm._fire_and_observe,
        command_type=dm.CommandType,
        contextlib_mod=dm.contextlib,
        evdev_mod=dm._combo_evdev_runtime(),
        uinput_writer=dm._combo_uinput_writer(),
    )


async def _runtime_combo_timeout_watchdog(manager: DeviceManager, deadline: float) -> None:
    await cdm.combo_timeout_watchdog(
        manager,
        deadline,
        asyncio_mod=dm._combo_asyncio_runtime(),
        time_mod=dm.time,
        action_type_enum=dm.ActionType,
        mapping_action_cls=dm.MappingAction,
        emit_mouse_move_fn=dm._combo_emit_mouse_move_fn(),
        get_trigger_axis_fn=dm.get_trigger_axis,
        resolve_code_fn=dm.resolve_output_code,
        fire_and_observe_fn=dm._fire_and_observe,
        command_type=dm.CommandType,
        contextlib_mod=dm.contextlib,
        evdev_mod=dm._combo_evdev_runtime(),
        uinput_writer=dm._combo_uinput_writer(),
    )


async def _runtime_run_macro_control_action(
    manager: DeviceManager, ev: dict[str, object], speed: float
) -> None:
    await mdm.run_macro_control_action(
        manager,
        ev,
        speed,
        asyncio_mod=dm._macro_asyncio_runtime(),
        contextlib_mod=dm.contextlib,
        random_mod=dm.random,
        uuid_mod=dm.uuid,
        command_type=dm._macro_command_type(),
        str_value_fn=dm._str_value,
        int_value_fn=dm._int_value,
    )


async def _runtime_process_grabbed_event(device: GrabbedDevice, event: evdev.InputEvent) -> None:
    await gde.process_event(
        device,
        event,
        evdev_mod=evdev,
        time_mod=gde.time,
        log=gdm.log,
        combo_decision_cls=ComboDecision,
        classify_event_device_type_fn=gde.classify_event_device_type,
        action_type_enum=ActionType,
    )


async def _runtime_execute_grabbed_action(
    device: GrabbedDevice,
    action: MappingAction,
    event: evdev.InputEvent | SimpleNamespace,
    event_name: str,
) -> None:
    await gda.execute_action(
        device,
        action,
        event,
        event_name,
        asyncio_mod=gdm.ASYNCIO_RUNTIME,
        command_type=dm.CommandType,
        fire_and_observe_fn=gde._fire_and_observe,
        action_type_enum=ActionType,
        superkey_machine_cls=gda.SuperkeyMachine,
        evdev_mod=evdev,
        uinput_writer=lambda device: cast(gdt.WritableUInput | None, device),
    )


async def _runtime_recover_grabbed_event_processing_error(device: GrabbedDevice) -> None:
    await gde.recover_from_event_processing_error(device)


async def _runtime_wait_for_grabbed_active_key_activity(
    device: GrabbedDevice,
    timeout_s: float,
) -> bool:
    return await gdg.wait_for_active_key_activity(
        device,
        timeout_s,
        asyncio_mod=gdm.ASYNCIO_RUNTIME,
        errno_mod=errno,
        log=gdm.log,
    )


async def _runtime_wait_for_grabbed_active_keys_to_clear(device: GrabbedDevice) -> None:
    await gdg.wait_for_active_keys_to_clear(
        device,
        asyncio_mod=gdm.ASYNCIO_RUNTIME,
        time_mod=gdm.time,
        log=gdm.log,
        active_key_idle_max_wait_s=gdm.ACTIVE_KEY_IDLE_MAX_WAIT_S,
        active_key_idle_log_interval_s=gdm.ACTIVE_KEY_IDLE_LOG_INTERVAL_S,
    )


def _runtime_find_grabbed_action_for_event(
    device: GrabbedDevice,
    event: evdev.InputEvent,
    mapping: dict[str, MappingAction],
) -> MappingAction | None:
    return gde.find_action_for_event(device, event, mapping)


def _runtime_write_grabbed_key(
    device: GrabbedDevice,
    uinput_dev: object | None,
    code: int,
    value: int,
) -> None:
    gdo.write_key(
        device,
        uinput_dev,
        code,
        value,
        evdev_mod=evdev,
        uinput_writer=lambda device: cast(gdt.WritableUInput | None, device),
    )


async def _runtime_tap_grabbed_key(
    device: GrabbedDevice,
    code: int,
    hold_ms: int,
    event_name: str,
    uinput_dev: object,
) -> None:
    await gdr.tap_key(
        device,
        code,
        hold_ms,
        event_name,
        uinput_dev,
        asyncio_mod=gdm.ASYNCIO_RUNTIME,
    )


async def _runtime_tap_grabbed_trigger(
    device: GrabbedDevice,
    axis_code: int,
    hold_ms: int,
    event_name: str,
) -> None:
    await gdr.tap_trigger(
        device,
        axis_code,
        hold_ms,
        event_name,
        asyncio_mod=gdm.ASYNCIO_RUNTIME,
        evdev_mod=evdev,
        uinput_writer=lambda device: cast(gdt.WritableUInput | None, device),
    )


async def _runtime_tap_grabbed_move(
    device: GrabbedDevice,
    action: MappingAction,
    event_name: str,
    hold_ms: int,
) -> None:
    await gdr.tap_move(device, action, event_name, hold_ms, asyncio_mod=gdm.ASYNCIO_RUNTIME)


async def _runtime_topology_watch_loop(manager: DeviceManager) -> None:
    await tdm.topology_watch_loop(
        dm._topology_manager(manager),
        asyncio_mod=dm._topology_asyncio_runtime(),
        cancelled_error=asyncio.CancelledError,
        log=dm.log,
        live_interface_info_cls=dm._topology_live_interface_info_factory(),
        clear_device_path_cache_fn=dm.clear_device_path_cache,
        device_paths_fn=dm._device_paths,
        device_input_fn=dm._topology_device_input_fn(),
        resolve_stable_path_fn=dm.resolve_stable_path,
        get_interface_id_fn=dm.get_interface_id,
    )


def _runtime_schedule_topology_reconcile(
    manager: DeviceManager,
    snapshot: dict[str, dm.LiveInterfaceInfo],
) -> None:
    tdm.schedule_topology_reconcile(
        dm._topology_manager(manager),
        snapshot,
        asyncio_mod=dm._topology_asyncio_runtime(),
        cancelled_error=asyncio.CancelledError,
        log=dm.log,
    )


def _runtime_parse_action(manager: DeviceManager, action: object) -> MappingAction:
    return adm.parse_action(
        manager,
        action,
        str_value=dm._str_value,
        optional_str=dm._optional_str,
        int_value=dm._int_value,
        int_or_none=dm._int_or_none,
        float_value=dm._float_value,
    )


def _runtime_schedule_hardware_release(
    manager: DeviceManager,
    hardware_id: str,
    grace_s: float | None,
) -> dict[str, object]:
    return ldm.schedule_hardware_release_unlocked(
        manager,
        hardware_id,
        grace_s,
        asyncio_mod=ldm.ASYNCIO_RUNTIME,
        log=dm.log,
    )


async def _runtime_release_device_unlocked(
    manager: DeviceManager,
    hardware_id: str,
) -> dict[str, object]:
    return await ldm.release_device_unlocked(manager, hardware_id, log=dm.log)


async def _runtime_delayed_interface_release(
    manager: DeviceManager,
    hardware_id: str,
    path: str,
    delay: float,
) -> None:
    await ldm.delayed_interface_release(
        manager,
        hardware_id,
        path,
        delay,
        asyncio_mod=ldm.ASYNCIO_RUNTIME,
    )


async def _runtime_release_interface_unlocked(
    manager: DeviceManager,
    hardware_id: str,
    path: str,
) -> None:
    await ldm.release_interface_unlocked(manager, hardware_id, path)


def _runtime_device_has_mapped_buttons(
    caps: dict[int, object],
    mapped_evdev_names: set[str],
    mapped_bindings: set[tuple[int, int]] | None,
) -> bool:
    return ldm.device_has_mapped_buttons(
        caps,
        mapped_evdev_names,
        mapped_bindings,
        evdev_mod=dm.evdev,
    )


async def _runtime_start_combo_action(
    manager: DeviceManager,
    combo_id: str,
    action: MappingAction,
    binding: dm.RuntimeComboBinding,
    *,
    trigger_bindings: tuple[dm.RuntimeComboBinding, ...] | None = None,
    resolve_code_fn: object = dm.resolve_output_code,
) -> None:
    await cdm.start_combo_action(
        manager,
        combo_id,
        action,
        binding,
        trigger_bindings or (binding,),
        action_type_enum=dm.ActionType,
        asyncio_mod=dm._combo_asyncio_runtime(),
        emit_mouse_move_fn=dm._combo_emit_mouse_move_fn(),
        get_trigger_axis_fn=dm.get_trigger_axis,
        resolve_code_fn=resolve_code_fn,
        fire_and_observe_fn=dm._fire_and_observe,
        command_type=dm.CommandType,
        evdev_mod=dm._combo_evdev_runtime(),
        uinput_writer=dm._combo_uinput_writer(),
    )


async def _runtime_stop_combo_action(manager: DeviceManager, combo_id: str) -> None:
    await cdm.stop_combo_action(
        manager,
        combo_id,
        asyncio_mod=dm._combo_asyncio_runtime(),
        contextlib_mod=dm.contextlib,
        mapping_action_cls=dm.MappingAction,
        evdev_mod=dm._combo_evdev_runtime(),
        uinput_writer=dm._combo_uinput_writer(),
        emit_mouse_move_fn=dm._combo_emit_mouse_move_fn(),
        get_trigger_axis_fn=dm.get_trigger_axis,
        resolve_code_fn=dm.resolve_output_code,
        fire_and_observe_fn=dm._fire_and_observe,
        command_type=dm.CommandType,
        action_type_enum=dm.ActionType,
    )

__all__ = [
    'asyncio',
    'contextlib',
    'errno',
    'logging',
    'os',
    'deque',
    'SimpleNamespace',
    'cast',
    'AsyncMock',
    'Mock',
    'evdev',
    'pytest',
    'CommandType',
    'ActionType',
    'DeviceType',
    'MappingAction',
    'SuperkeyMode',
    'dm',
    'ComboDecision',
    'ComboInputEvent',
    'DesiredGrabConfig',
    'DeviceManager',
    'adm',
    'cdm',
    'ldm',
    'gdm',
    'gda',
    'gde',
    'gdg',
    'gdo',
    'gdr',
    'gdt',
    'mdm',
    'tdm',
    'GrabbedDevice',
    'SuperkeyActionData',
    'SuperkeyConfig',
    '_FakeUInput',
    '_make_grabbed_device',
    '_runtime_on_device_event',
    '_runtime_clear_combo_runtime',
    '_runtime_clear_combo_scope',
    '_runtime_refresh_combo_watchdog',
    '_runtime_combo_timeout_watchdog',
    '_runtime_run_macro_control_action',
    '_runtime_process_grabbed_event',
    '_runtime_execute_grabbed_action',
    '_runtime_recover_grabbed_event_processing_error',
    '_runtime_wait_for_grabbed_active_key_activity',
    '_runtime_wait_for_grabbed_active_keys_to_clear',
    '_runtime_find_grabbed_action_for_event',
    '_runtime_write_grabbed_key',
    '_runtime_tap_grabbed_key',
    '_runtime_tap_grabbed_trigger',
    '_runtime_tap_grabbed_move',
    '_runtime_topology_watch_loop',
    '_runtime_schedule_topology_reconcile',
    '_runtime_parse_action',
    '_runtime_schedule_hardware_release',
    '_runtime_release_device_unlocked',
    '_runtime_delayed_interface_release',
    '_runtime_release_interface_unlocked',
    '_runtime_device_has_mapped_buttons',
    '_runtime_start_combo_action',
    '_runtime_stop_combo_action',
]
