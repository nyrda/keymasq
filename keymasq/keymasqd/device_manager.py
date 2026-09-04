import asyncio
import errno
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, Protocol, cast

import evdev

from keymasq.common.coercion import (
    coerce_int,
    coerce_str,
    json_object,
)
from keymasq.common.devices import (
    clear_device_path_cache,
    detect_input_classes,
    get_interface_id,
    primary_input_class,
    resolve_stable_path,
)
from keymasq.common.ipc import CommandType
from keymasq.common.model.actions import MappingAction, parse_profile_deactivation_policy
from keymasq.common.model.core import DeviceType
from keymasq.common.types import JsonObject
from keymasq.common.virtual_devices import (
    clamp_virtual_gamepad_count,
)
from keymasq.keymasqd import device_inventory
from keymasq.keymasqd.combo_engine import ComboDecision
from keymasq.keymasqd.permission_hints import (
    input_device_permission_message,
    is_permission_error,
)
from keymasq.keymasqd.recording import RecordingManager
from keymasq.keymasqd.runtime import (
    adapters,
    device_inspector,
    device_path_resolver,
    diagnostics,
    outputs,
    repeat,
    topology,
    virtual_gamepads,
)
from keymasq.keymasqd.runtime.combo import lifecycle
from keymasq.keymasqd.runtime.grab import support
from keymasq.keymasqd.runtime.grab.acquisition import grab_device_unlocked
from keymasq.keymasqd.runtime.grab.mapping import set_mapping
from keymasq.keymasqd.runtime.grab.release import (
    release_all_devices,
    release_device_unlocked,
    release_interface_unlocked,
    schedule_hardware_release_unlocked,
)
from keymasq.keymasqd.runtime.grab.state import (
    DesiredGrabConfig,
    GrabDeviceDeps,
    GrabRequest,
    GrabRuntimeState,
)
from keymasq.keymasqd.runtime.grabbed_device.device import GrabbedDevice
from keymasq.keymasqd.runtime.grabbed_device.event import pipeline
from keymasq.keymasqd.runtime.macro.state import (
    DiagnosticRecorder,
    MacroRuntimeDeps,
    MacroRuntimeState,
)
from keymasq.keymasqd.runtime.manager_combos import ComboManagerMixin
from keymasq.keymasqd.runtime.manager_cursor import CursorManagerMixin
from keymasq.keymasqd.runtime.manager_macros import MacroManagerMixin
from keymasq.keymasqd.runtime.profile_activation_tracker import ProfileActivationTracker
from keymasq.keymasqd.task_helpers import fire_and_observe

log = logging.getLogger("keymasqd.devices")
ACTIVE_KEY_IDLE_LOG_INTERVAL_S = 1.0
ACTIVE_KEY_IDLE_MAX_WAIT_S = 300.0
COMBO_HELD_REARM_MODIFIERS = frozenset({"shift", "ctrl", "alt", "meta"})
TOPOLOGY_POLL_INTERVAL_S = 0.5
TOPOLOGY_DEBOUNCE_S = 0.5
type BroadcastCallback = Callable[[CommandType, JsonObject], Awaitable[None]]
type MappingGetter = Callable[[], dict[str, MappingAction]]
type DeviceEventCallback = Callable[..., Awaitable[ComboDecision | bool | None]]
type MacroPlayer = Callable[..., Awaitable[JsonObject]]
type RapidfireTaskFactory = Callable[[], asyncio.Task[None]]


class _ManagedInputDevice(Protocol):
    path: str
    name: str | None
    info: adapters.DeviceInfo

    def grab(self) -> None: ...

    def ungrab(self) -> None: ...

    def capabilities(self) -> dict[int, Sequence[object]]: ...

    def input_props(self) -> Sequence[int]: ...

    def async_read_loop(self) -> AsyncIterator[evdev.InputEvent]: ...

    def fileno(self) -> int: ...

    def read_one(self) -> evdev.InputEvent | None: ...

    def active_keys(self) -> Sequence[int]: ...

    def close(self) -> None: ...


def _device_input(path: str) -> _ManagedInputDevice:
    return cast(_ManagedInputDevice, evdev.InputDevice(path))


def _device_paths() -> list[str]:
    return cast(Callable[[], list[str]], evdev.list_devices)()


def _topology_runtime_deps() -> topology.TopologyRuntimeDeps:
    return topology.TopologyRuntimeDeps(
        asyncio_mod=adapters.ASYNCIO_RUNTIME,
        clear_device_path_cache_fn=clear_device_path_cache,
        device_paths_fn=_device_paths,
        device_input_fn=_device_input,
        detect_input_classes_fn=detect_input_classes,
        primary_input_class_fn=primary_input_class,
        resolve_stable_path_fn=resolve_stable_path,
        get_interface_id_fn=get_interface_id,
        release_interface_fn=release_interface_unlocked,
    )


def _device_path_resolver_deps() -> device_path_resolver.DevicePathResolverDeps:
    return device_path_resolver.evdev_device_path_resolver_deps(_device_input)


def _macro_runtime_deps(
    diagnostics_recorder: DiagnosticRecorder | None = None,
) -> MacroRuntimeDeps:
    return MacroRuntimeDeps(
        asyncio_mod=adapters.ASYNCIO_RUNTIME,
        evdev_mod=evdev,
        uinput_writer=adapters.identity_uinput_writer,
        log=log,
        int_value_fn=coerce_int,
        str_value_fn=coerce_str,
        diagnostics_recorder=diagnostics_recorder,
    )


class DeviceManager(CursorManagerMixin, MacroManagerMixin, ComboManagerMixin):
    def __init__(
        self,
        verbosity: int = 0,
        broadcast_callback: BroadcastCallback | None = None,
        release_grace_s: float = 60.0,
        held_release_retry_s: float = 10.0,
        topology_poll_s: float = TOPOLOGY_POLL_INTERVAL_S,
        topology_debounce_s: float = TOPOLOGY_DEBOUNCE_S,
    ) -> None:
        self.grabbed_devices: dict[str, list[GrabbedDevice]] = {}
        self.active_mappings: dict[str, dict[str, MappingAction]] = {}
        self.verbosity = verbosity
        self.broadcast_callback = broadcast_callback

        self.output_state = outputs.OutputRuntimeState()
        self._gamepad_output_router = virtual_gamepads.GamepadOutputRouter(log)
        self.recording_manager: RecordingManager | None = None
        self.macro_store: Any | None = None
        self._initialize_macro_runtime(self._macro_runtime_deps_with_diagnostics)
        self.macro_exec_timeout_max_ms = 30000
        self.macro_state = MacroRuntimeState()
        self._op_lock = asyncio.Lock()
        self._neutralize_lock = asyncio.Lock()
        self._runtime_input_paused = False
        self._initialize_cursor_runtime()
        self._diagnostics = diagnostics.DiagnosticsRuntime(log)
        self.diagnostics_state = self._diagnostics.state
        self.grab_state = GrabRuntimeState(
            release_grace_s=max(0.01, float(release_grace_s)),
            held_release_retry_s=max(0.01, float(held_release_retry_s)),
        )
        self._initialize_combo_runtime()
        self.repeat_state = repeat.RepeatRuntimeState()
        self.profile_activation_tracker = ProfileActivationTracker(
            broadcast_deactivate_request=self._broadcast_profile_deactivate_requested,
        )
        self.device_inspector_state = device_inspector.DeviceInspectorState()
        self.topology_state = topology.TopologyRuntimeState(
            poll_s=max(0.05, float(topology_poll_s)),
            debounce_s=max(0.05, float(topology_debounce_s)),
        )
        self._command_type = CommandType
        self._desired_grab_config_cls = DesiredGrabConfig
        self._device_input = _device_input

    def initialize_output_devices(self) -> None:
        outputs.create_global_uinputs(
            cast(Any, self),
            evdev_mod=evdev,  # pyright: ignore[reportArgumentType]
            log=log,
            uinput_writer=adapters.identity_uinput_writer,
        )

    def shutdown_output_devices(self) -> None:
        outputs.destroy_global_uinputs(cast(Any, self), log=log)

    async def set_virtual_gamepads(self, count: object) -> JsonObject:
        clamped_count = clamp_virtual_gamepad_count(count)
        async with self._op_lock:

            async def clear_runtime() -> None:
                await lifecycle.clear_combo_runtime(
                    self,
                    deps=support.combo_runtime_deps(),
                )

            def configure_outputs(configured_count: int) -> None:
                outputs.configure_virtual_gamepads(
                    cast(Any, self),
                    configured_count,
                    evdev_mod=evdev,  # pyright: ignore[reportArgumentType]
                    log=log,
                    uinput_writer=adapters.identity_uinput_writer,
                )

            return await virtual_gamepads.reconfigure_virtual_gamepads(
                count=clamped_count,
                current_count=self.output_state.virtual_gamepad_count,
                output_devices_active=self.output_state.device_count > 0,
                grabbed_devices=cast(Any, self.grabbed_devices),
                clear_combo_runtime=clear_runtime,
                configure_outputs=configure_outputs,
                set_inactive_count=lambda value: setattr(
                    self.output_state, "virtual_gamepad_count", value
                ),
                logger=log,
            )

    def resolve_gamepad_output(
        self,
        output_id: str | None,
        *,
        context: str = "",
    ) -> virtual_gamepads.GamepadOutputTarget | None:
        return self._gamepad_output_router.resolve(
            self.output_state,
            cast(Any, self.grabbed_devices),
            output_id,
            context=context,
        )

    async def start_topology_watcher(self) -> None:
        await topology.start_topology_watcher(
            self,
            log=log,
            deps=_topology_runtime_deps(),
        )

    async def stop_topology_watcher(self) -> None:
        await topology.stop_topology_watcher(
            self,
            deps=_topology_runtime_deps(),
        )

    async def grab_device(
        self,
        hardware_id: str,
        evdev_paths: list[str],
        button_map: dict[str, str],
        button_codes: dict[str, int] | None = None,
        button_values: dict[str, int] | None = None,
        analog_inputs: dict[str, object] | None = None,
        motion_sensors: dict[str, object] | None = None,
        force_grab_unmapped: bool = False,
        evdev_interfaces: list[JsonObject] | None = None,
    ) -> JsonObject:
        async with self._op_lock:
            request = GrabRequest(
                hardware_id=hardware_id,
                evdev_paths=evdev_paths,
                button_map=button_map,
                button_codes=button_codes,
                button_values=button_values,
                analog_inputs=analog_inputs,
                motion_sensors=motion_sensors,
                force_grab_unmapped=force_grab_unmapped,
                evdev_interfaces=evdev_interfaces,
                update_desired=True,
            )
            deps = GrabDeviceDeps(
                desired_grab_config_cls=DesiredGrabConfig,
                clear_device_path_cache_fn=clear_device_path_cache,
                resolve_stable_path_fn=resolve_stable_path,
                device_path_resolver_deps=_device_path_resolver_deps(),
                grabbed_device_cls=GrabbedDevice,
                get_interface_id_fn=get_interface_id,
                str_value_fn=coerce_str,
                int_value_fn=coerce_int,
                fire_and_observe_fn=fire_and_observe,
                errno_mod=errno,
            )
            result = await grab_device_unlocked(
                self,
                request,
                deps,
            )
            await self._refresh_combo_runtime_preserving_unchanged()
            return result

    async def release_device(
        self,
        hardware_id: str,
        immediate: bool = False,
        grace_s: float | None = None,
    ) -> JsonObject:
        async with self._op_lock:
            if immediate:
                result = await release_device_unlocked(
                    self,
                    hardware_id,
                    log=log,
                )
                await self._refresh_combo_runtime_preserving_unchanged()
                return result
            return await schedule_hardware_release_unlocked(
                self,
                hardware_id,
                grace_s,
                asyncio_mod=adapters.ASYNCIO_RUNTIME,
                log=log,
            )

    async def release_all_devices(self) -> None:
        self.profile_activation_tracker.reset()
        self.repeat_state.history.clear()
        await release_all_devices(
            self,
            fire_and_observe_fn=fire_and_observe,
        )
        async with self._op_lock:
            self.device_inspector_state.reset()
            await self._refresh_combo_runtime()

    def runtime_input_paused(self) -> bool:
        return self._runtime_input_paused

    def pause_runtime_input(self) -> None:
        self._runtime_input_paused = True

    def resume_runtime_input(self) -> None:
        self._runtime_input_paused = False

    async def neutralize_runtime(self) -> JsonObject:
        """Stop active input runtimes and release every tracked output."""

        async with self._neutralize_lock:
            was_paused = self._runtime_input_paused
            self._runtime_input_paused = True
            devices = [
                device
                for grabbed in list(self.grabbed_devices.values())
                for device in list(grabbed)
            ]
            errors: list[Exception] = []
            combo_deps = support.combo_runtime_deps()

            async def attempt(label: str, cleanup: Callable[[], Awaitable[object]]) -> None:
                try:
                    await cleanup()
                except Exception as exc:
                    errors.append(exc)
                    log.exception("Runtime neutralization failed while %s", label)

            def attempt_sync(label: str, cleanup: Callable[[], object]) -> None:
                try:
                    cleanup()
                except Exception as exc:
                    errors.append(exc)
                    log.exception("Runtime neutralization failed while %s", label)

            try:
                await attempt("cancelling macro playback", self.cancel_macro_playback)
                await attempt(
                    "clearing combo runtime",
                    lambda: lifecycle.clear_combo_runtime(self, deps=combo_deps),
                )
                await attempt("cancelling cursor movement", self.cancel_cursor_move)

                for device in devices:
                    held_sources = set(device.state.held_source_keys)
                    held_sources.update(device.state.held_source_actions)
                    held_sources.update(device.state.combo_passthrough_held)
                    device.state.quarantined_source_keys.update(held_sources)

                    attempt_sync(
                        f"resetting motion controls for {device.path}",
                        device.reset_motion_controls,
                    )
                    await attempt(
                        f"resetting analog controls for {device.path}",
                        device.reset_analog_controls,
                    )
                    await attempt(
                        f"resetting superkeys for {device.path}",
                        device.reset_superkeys,
                    )

                await attempt("cancelling teardown macro playback", self.cancel_macro_playback)

                for device in devices:
                    attempt_sync(
                        f"ending held profile triggers for {device.path}",
                        lambda device=device: pipeline.observe_profile_trigger_end_for_held_sources(
                            device
                        ),
                    )
                    device.state.repeat_active_actions.clear()
                    device.state.passthrough_frame_output = None
            finally:
                attempt_sync(
                    "releasing combo outputs",
                    lambda: lifecycle.release_tracked_outputs(self, deps=combo_deps),
                )
                for device in devices:
                    attempt_sync(
                        f"releasing tracked outputs for {device.path}",
                        device.release_tracked_outputs,
                    )
                if not was_paused:
                    self._runtime_input_paused = False

            if errors:
                raise errors[0]
            return {"status": "ok", "neutralized": True}

    async def emergency_reset(self) -> JsonObject:
        await self.release_all_devices()
        self._broadcast_runtime_event(
            CommandType.RUNTIME_RESET,
            {"reason": "emergency_reset"},
        )
        return {"status": "ok", "reset": True}

    def _broadcast_runtime_event(
        self,
        event_type: CommandType,
        data: JsonObject,
    ) -> None:
        if self.broadcast_callback is None:
            return
        fire_and_observe(
            self.broadcast_callback(event_type, data),
            f"{event_type.value} broadcast",
        )

    def _broadcast_profile_deactivate_requested(self, data: JsonObject) -> None:
        self._broadcast_runtime_event(CommandType.PROFILE_DEACTIVATE_REQUESTED, data)

    async def track_profile_activation(
        self,
        profile_name: str,
        activation_id: str,
        trigger_id: str,
        deactivation: object,
    ) -> JsonObject:
        policy = parse_profile_deactivation_policy(deactivation)
        if policy is None:
            return {"status": "ok", "tracked": False}
        self.profile_activation_tracker.track(
            profile_name=profile_name,
            activation_id=activation_id,
            trigger_id=trigger_id,
            deactivation=policy,
        )
        return {
            "status": "ok",
            "tracked": True,
            "profile_name": profile_name,
            "activation_id": activation_id,
        }

    async def cancel_profile_activation(
        self,
        profile_name: str = "",
        activation_id: str = "",
    ) -> JsonObject:
        self.profile_activation_tracker.cancel(
            profile_name=profile_name or None,
            activation_id=activation_id or None,
        )
        return {
            "status": "ok",
            "profile_name": profile_name,
            "activation_id": activation_id,
        }

    def observe_profile_trigger_start(self, trigger_id: str | None) -> None:
        self.profile_activation_tracker.observe_trigger_start(trigger_id)

    def observe_profile_trigger_end(self, trigger_id: str | None) -> None:
        self.profile_activation_tracker.observe_trigger_end(trigger_id)

    def record_profile_action(
        self,
        source_profile_name: str | None = None,
        trigger_id: str | None = None,
    ) -> None:
        self.profile_activation_tracker.record_action(source_profile_name, trigger_id)

    def device_inspector_active(self, hardware_id: str) -> bool:
        return self.device_inspector_state.is_active(hardware_id)

    def device_inspector_suppressed(self, hardware_id: str) -> bool:
        return self.device_inspector_state.is_suppressed(hardware_id)

    def device_inspector_suppressed_hardware_ids_snapshot(self) -> set[str]:
        return self.device_inspector_state.suppressed_snapshot()

    def broadcast_device_inspector_event(self, payload: JsonObject) -> None:
        event_payload = self.device_inspector_state.event_payload(payload)
        if event_payload is not None:
            self._broadcast_runtime_event(CommandType.DEVICE_INSPECTOR_EVENT, event_payload)

    def _broadcast_device_inspector_status(self, hardware_id: str, reason: str) -> None:
        self._broadcast_runtime_event(
            CommandType.DEVICE_INSPECTOR_STATUS,
            self.device_inspector_state.status_payload(hardware_id, reason),
        )

    async def start_device_inspector(self, hardware_id: str) -> JsonObject:
        async with self._op_lock:
            transition = self.device_inspector_state.start(hardware_id)
            self._broadcast_device_inspector_status(transition.hardware_id, "start")
            return transition.response()

    async def stop_device_inspector(self, hardware_id: str) -> JsonObject:
        async with self._op_lock:
            transition = self.device_inspector_state.stop(hardware_id)
            if transition.reset_runtime:
                await self._reset_device_inspector_runtime_unlocked(transition.hardware_id)
                await self._refresh_combo_runtime()
            self._broadcast_device_inspector_status(transition.hardware_id, "stop")
            return transition.response()

    async def enable_device_inspector_suppression(self, hardware_id: str) -> JsonObject:
        async with self._op_lock:
            transition = self.device_inspector_state.enable_suppression(hardware_id)
            await self._reset_device_inspector_runtime_unlocked(transition.hardware_id)
            await self._refresh_combo_runtime()
            self._broadcast_device_inspector_status(transition.hardware_id, "enable_suppression")
            return transition.response()

    async def disable_device_inspector_suppression(
        self,
        hardware_id: str,
        reason: str = "manual",
    ) -> JsonObject:
        async with self._op_lock:
            transition = self.device_inspector_state.disable_suppression(hardware_id)
            if transition.reset_runtime:
                await self._reset_device_inspector_runtime_unlocked(transition.hardware_id)
                await self._refresh_combo_runtime()
            self._broadcast_device_inspector_status(transition.hardware_id, reason)
            return transition.response(reason=str(reason or ""))

    async def _reset_device_inspector_runtime_unlocked(self, hardware_id: str) -> None:
        for device in self.grabbed_devices.get(hardware_id, []):
            release_tracked_outputs = getattr(device, "release_tracked_outputs", None)
            if callable(release_tracked_outputs):
                release_tracked_outputs()
            reset_mapping_runtime_state = getattr(device, "reset_mapping_runtime_state", None)
            if callable(reset_mapping_runtime_state):
                await cast(Awaitable[object], reset_mapping_runtime_state())

    async def set_mapping(
        self,
        hardware_id: str,
        mapping: JsonObject,
    ) -> JsonObject:
        result = await set_mapping(
            self,
            hardware_id,
            mapping,
            json_object_fn=json_object,
            log=log,
        )
        repeat.forget_exec_actions(
            self.repeat_state,
            source_device=hardware_id,
            exclude_source_button_prefix="combo:",
        )
        return result

    async def set_diagnostics(
        self,
        enabled: bool,
        interval: float = 5.0,
        categories: Sequence[object] | None = None,
    ) -> JsonObject:
        return await self._diagnostics.configure(
            enabled,
            interval,
            categories,
            loop_factory=self._diagnostics_loop,
        )

    def _record_diagnostic(self, label: str, duration_us: float) -> None:
        self._diagnostics.record(label, duration_us)

    def _macro_runtime_deps_with_diagnostics(self) -> MacroRuntimeDeps:
        recorder = (
            self._record_diagnostic
            if self.diagnostics_state.enabled
            and diagnostics.label_enabled("macro_load", self.diagnostics_state.categories)
            else None
        )
        return _macro_runtime_deps(recorder)

    async def _diagnostics_loop(self) -> None:
        await self._diagnostics.run(
            sleep=asyncio.sleep,
            to_thread=asyncio.to_thread,
            summarize_snapshot=self._summarize_diagnostics_snapshot,
            publish_summary=self._broadcast_diagnostics_snapshot,
            write_summary=self._log_diagnostics_summary,
        )

    def _summarize_diagnostics_snapshot(
        self,
        snapshot: dict[str, list[float]],
    ) -> dict[str, JsonObject]:
        return diagnostics.summarize(snapshot)

    def _broadcast_diagnostics_snapshot(self, summary: dict[str, JsonObject]) -> None:
        if not summary or self.broadcast_callback is None:
            return
        self._broadcast_runtime_event(
            CommandType.DIAGNOSTICS_SNAPSHOT,
            {
                "enabled": True,
                "interval": self.diagnostics_state.interval,
                "categories": sorted(self.diagnostics_state.categories),
                "samples": summary,
            },
        )

    def _log_diagnostics_summary(self, summary: dict[str, JsonObject]) -> None:
        diagnostics.log_summary(log, summary)

    async def list_devices(self) -> JsonObject:
        return await asyncio.to_thread(self._list_devices_sync)

    async def device_runtime_status(self) -> JsonObject:
        async with self._op_lock:
            return device_inventory.runtime_status(
                cast(Any, self.topology_state.live_snapshot),
                cast(Any, self.grabbed_devices),
            )

    def _list_devices_sync(self) -> JsonObject:
        deps = device_inventory.InventoryScanDeps(
            clear_path_cache=clear_device_path_cache,
            device_paths=_device_paths,
            open_device=cast(Any, _device_input),
            resolve_stable_path=resolve_stable_path,
            get_interface_id=get_interface_id,
            detect_device_types=cast(Any, self._detect_device_types),
            primary_input_class=primary_input_class,
            evdev_mod=evdev,
            is_permission_error=is_permission_error,
            permission_message=input_device_permission_message,
            logger=log,
        )
        return device_inventory.scan_devices(
            deps,
            virtual_metadata=self._recording_virtual_device_metadata(),
            grabbed_metadata=self._recording_grabbed_source_metadata(),
        )

    def _recording_virtual_device_metadata(self) -> dict[str, JsonObject]:
        return device_inventory.recording_virtual_device_metadata(
            self.output_state,
            cast(Any, self.grabbed_devices),
        )

    def _recording_grabbed_source_metadata(self) -> dict[str, JsonObject]:
        return device_inventory.recording_grabbed_source_metadata(cast(Any, self.grabbed_devices))

    def _detect_device_types(self, device: _ManagedInputDevice) -> list[str]:
        return detect_input_classes(device)

    def _detect_device_type(self, device: _ManagedInputDevice) -> DeviceType:
        return primary_input_class(self._detect_device_types(device))
