import asyncio
import logging
import queue
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import ClassVar, Protocol, cast

from keymasq.common.combos import normalize_combo_evdev
from keymasq.common.ipc import CommandType
from keymasq.common.models import (
    ActionType,
    MappingAction,
    SuperkeyMode,
    combo_effective_superkey_config,
)
from keymasq.keymasqd.combo_engine import (
    ComboActionTransition,
    ComboDecision,
    ComboEngine,
    ComboInputEvent,
    ComboSyntheticEvent,
    RuntimeCombo,
    RuntimeComboBinding,
)
from keymasq.keymasqd.runtime.action_runner import (
    build_action_trigger_payload,
    build_macro_playback_request,
    dispatch_action_trigger,
    is_hold_macro_action,
)
from keymasq.keymasqd.runtime.mouse_actions import (
    rapidfire_relative_pulses,
    resolve_mouse_output_target,
    tap_relative_pulse,
    write_relative_pulse,
)
from keymasq.keymasqd.superkey_state import SuperkeyConfig as RuntimeSuperkeyConfig
from keymasq.keymasqd.superkey_state import SuperkeyMachine

log = logging.getLogger("keymasqd.runtime.combos")

type JsonObject = dict[str, object]
type IntValueFn = Callable[..., int]
type StrValueFn = Callable[..., str]
type ResolveStablePathFn = Callable[[str], str]
type GetInterfaceIdFn = Callable[[str], str | None]
type ResolveCodeFn = Callable[[str], int | None]
type TriggerAxisFn = Callable[[str], tuple[bool, int | None]]
type FireAndObserve = Callable[[Awaitable[object], str], asyncio.Task[object]]

type ComboCaptureQueueState = tuple[
    queue.SimpleQueue[dict[str, object]], set[str], asyncio.Event | None
]


class _WritableUInput(Protocol):
    def write(self, event_type: int, code: int, value: int) -> None: ...

    def syn(self) -> None: ...


type UInputWriter = Callable[[object | None], _WritableUInput | None]


class _EcodesByType(Protocol):
    def get(self, key: int, default: dict[int, object] | None = None) -> dict[int, object]: ...


class _Ecodes(Protocol):
    EV_KEY: int
    EV_REL: int
    EV_ABS: int
    bytype: _EcodesByType


class _EvdevModule(Protocol):
    ecodes: _Ecodes


class _TimeModule(Protocol):
    def monotonic(self) -> float: ...


class _AsyncioModule(Protocol):
    CancelledError: ClassVar[type[BaseException]]

    def create_task(self, coro: Awaitable[None], /) -> asyncio.Task[None]: ...

    async def sleep(self, delay: float, /) -> None: ...

    def current_task(self) -> asyncio.Task[None] | None: ...


class _ContextlibModule(Protocol):
    def suppress(self, *exceptions: type[BaseException]) -> AbstractContextManager[None]: ...


class _EmitMouseMoveFn(Protocol):
    def __call__(
        self,
        uinput_dev: object | None,
        move_x: int,
        move_y: int,
        *,
        absolute: bool = False,
    ) -> None: ...


class _QueueFactory(Protocol):
    def __call__(self) -> queue.SimpleQueue[dict[str, object]]: ...


class _QueueModule(Protocol):
    Empty: ClassVar[type[BaseException]]
    SimpleQueue: _QueueFactory


class _OutputState(Protocol):
    @property
    def keyboard_uinput(self) -> object | None: ...

    @property
    def mouse_uinput(self) -> object | None: ...

    @property
    def gamepad_uinput(self) -> object | None: ...


class _GrabbedComboDevice(Protocol):
    hardware_id: str
    interface_id: str

    def emit_combo_release(self, evdev_name: str) -> None: ...

    def emit_combo_press(self, evdev_name: str) -> None: ...

    def combo_passthrough_binding_active(self, evdev_name: str) -> bool: ...

    def combo_source_binding_held(self, evdev_name: str) -> bool: ...

    def combo_binding_recalled(self, evdev_name: str) -> bool: ...

    def mark_combo_recalled_binding(self, evdev_name: str) -> None: ...

    def clear_combo_recalled_binding(self, evdev_name: str) -> None: ...

    def combo_passthrough_held_modifiers(self) -> set[str]: ...

    def combo_held_source_bindings(self) -> set[str]: ...


class _ComboManager(Protocol):
    @property
    def combo_state(self) -> "ComboRuntimeState": ...

    @property
    def output_state(self) -> _OutputState: ...

    @property
    def grabbed_devices(self) -> Mapping[str, Sequence[_GrabbedComboDevice]]: ...

    @property
    def broadcast_callback(self) -> Callable[[CommandType, JsonObject], Awaitable[None]] | None: ...

    async def set_cursor_position(self, x: int, y: int) -> JsonObject: ...

    async def play_macro(
        self,
        *,
        macro_events: list[JsonObject],
        macro_name: str = "",
        replay_mouse_movement: bool = True,
        replay_mouse_clicks: bool = True,
        speed: float = 1.0,
        loop_mode: str = "none",
        loop_count: int = 1,
        move_to_start: bool = False,
        start_x: int = 0,
        start_y: int = 0,
        block_mouse_movement: bool = False,
        source_device: str = "",
        source_button: str = "",
        trigger_value: int = 1,
    ) -> JsonObject: ...


@dataclass
class ComboActionState:
    kind: str
    code: int | None = None
    axis_code: int | None = None
    uinput: object | None = None
    active: bool = False
    task: asyncio.Task[None] | None = None
    action: MappingAction | None = None
    source_device: str | None = None
    source_button: str | None = None
    child_combo_ids: list[str] = field(default_factory=list)
    machine: SuperkeyMachine | None = None
    recalled_bindings: list[RuntimeComboBinding] = field(default_factory=list)
    restore_bindings: list[RuntimeComboBinding] = field(default_factory=list)


@dataclass
class ComboRuntimeState:
    capture_queues: dict[str, ComboCaptureQueueState] = field(default_factory=dict)
    active_combos: list[RuntimeCombo] = field(default_factory=list)
    engine: ComboEngine = field(default_factory=ComboEngine)
    timeout_task: asyncio.Task[None] | None = None
    active_actions: dict[str, ComboActionState] = field(default_factory=dict)
    superkey_machines: dict[str, SuperkeyMachine] = field(default_factory=dict)
    held_output_keys: dict[str, set[int]] = field(
        default_factory=lambda: {
            "keyboard": set(),
            "mouse": set(),
            "gamepad": set(),
        }
    )
    superkey_output_refcounts: dict[str, dict[int, int]] = field(
        default_factory=lambda: {
            "keyboard": {},
            "mouse": {},
            "gamepad": {},
        }
    )


@dataclass(frozen=True)
class ComboRuntimeDeps:
    asyncio_mod: _AsyncioModule
    contextlib_mod: _ContextlibModule
    time_mod: _TimeModule
    evdev_mod: _EvdevModule
    uinput_writer: UInputWriter
    emit_mouse_move_fn: _EmitMouseMoveFn
    get_trigger_axis_fn: TriggerAxisFn
    resolve_code_fn: ResolveCodeFn
    fire_and_observe_fn: FireAndObserve


def _evdev_code_name(raw_name: object, fallback: int) -> str:
    if isinstance(raw_name, tuple):
        names = cast(tuple[object, ...], raw_name)
        first: object = names[0] if names else str(fallback)
        return str(first).lower()
    return str(raw_name).lower()


async def on_device_event(
    manager: _ComboManager,
    hardware_id: str,
    evdev_path: str,
    event_type: int,
    event_code: int,
    event_value: int,
    stable_path: str | None,
    source: str | None,
    *,
    resolve_stable_path_fn: ResolveStablePathFn,
    get_interface_id_fn: GetInterfaceIdFn,
    combo_binding_cls: type[RuntimeComboBinding],
    combo_input_event_cls: type[ComboInputEvent],
    int_value_fn: IntValueFn,
    str_value_fn: StrValueFn,
    deps: ComboRuntimeDeps,
) -> ComboDecision | bool | None:
    combo_payload = build_combo_event_payload(
        hardware_id,
        evdev_path,
        event_type,
        event_code,
        event_value,
        stable_path=stable_path,
        source=source,
        evdev_mod=deps.evdev_mod,
        resolve_stable_path_fn=resolve_stable_path_fn,
        get_interface_id_fn=get_interface_id_fn,
    )
    capture_active = queue_combo_capture_event(manager, combo_payload, str_value_fn=str_value_fn)
    if capture_active:
        return True
    return await process_runtime_combo_event(
        manager,
        combo_payload,
        combo_binding_cls=combo_binding_cls,
        combo_input_event_cls=combo_input_event_cls,
        int_value_fn=int_value_fn,
        str_value_fn=str_value_fn,
        deps=deps,
    )


def build_combo_event_payload(
    hardware_id: str,
    evdev_path: str,
    event_type: int,
    event_code: int,
    event_value: int,
    *,
    stable_path: str | None,
    source: str | None,
    evdev_mod: _EvdevModule,
    resolve_stable_path_fn: ResolveStablePathFn,
    get_interface_id_fn: GetInterfaceIdFn,
) -> dict[str, object] | None:
    if event_type != evdev_mod.ecodes.EV_KEY or int(event_value) not in {0, 1, 2}:
        return None

    raw_code_name: object = evdev_mod.ecodes.bytype.get(event_type, {}).get(
        event_code, str(event_code)
    )
    evdev_name = _evdev_code_name(raw_code_name, event_code)
    if not evdev_name.startswith(("key_", "btn_")):
        return None

    resolved_stable_path = stable_path or resolve_stable_path_fn(evdev_path)
    return {
        "evdev": evdev_name,
        "code": int(event_code),
        "value": int(event_value),
        "source": str(source or get_interface_id_fn(resolved_stable_path) or "").lower(),
        "stable_path": resolved_stable_path,
        "device_path": evdev_path,
        "hardware_id": str(hardware_id).lower(),
    }


def queue_combo_capture_event(
    manager: _ComboManager,
    payload: dict[str, object] | None,
    *,
    str_value_fn: StrValueFn,
) -> bool:
    if payload is None or not manager.combo_state.capture_queues:
        return False
    hardware_id = str_value_fn(payload.get("hardware_id"), "")
    for capture_queue, hardware_ids, notify_event in manager.combo_state.capture_queues.values():
        if hardware_ids and hardware_id not in hardware_ids:
            continue
        capture_queue.put(dict(payload))
        if notify_event is not None:
            notify_event.set()
    return True


async def process_runtime_combo_event(
    manager: _ComboManager,
    payload: dict[str, object] | None,
    *,
    combo_binding_cls: type[RuntimeComboBinding],
    combo_input_event_cls: type[ComboInputEvent],
    int_value_fn: IntValueFn,
    str_value_fn: StrValueFn,
    deps: ComboRuntimeDeps,
) -> ComboDecision | None:
    if payload is None or not manager.combo_state.active_combos:
        return None

    raw_value = payload.get("value")
    value = int_value_fn(raw_value, -1) if raw_value is not None else -1
    if value not in {0, 1, 2}:
        return None

    binding = combo_binding_cls(
        hardware_id=str_value_fn(payload.get("hardware_id"), ""),
        evdev=str_value_fn(payload.get("evdev"), ""),
        source=str_value_fn(payload.get("source"), ""),
    )
    if value == 1:
        held_modifiers = held_combo_modifier_bindings_for_scope(
            manager,
            binding.hardware_id,
            binding.source,
            combo_binding_cls=combo_binding_cls,
        )
        if binding in held_modifiers:
            held_modifiers.discard(binding)
        manager.combo_state.engine.prime_held_bindings(held_modifiers)
    decision = manager.combo_state.engine.handle_event(
        combo_input_event_cls(binding=binding, value=value),
        deps.time_mod.monotonic(),
    )
    if decision.recall_events:
        emit_combo_recalls(manager, decision.recall_events)
    if decision.action_transition is not None:
        await apply_combo_action_transition(
            manager,
            decision.action_transition,
            deps=deps,
        )
    for transition in decision.extra_action_transitions:
        await apply_combo_action_transition(
            manager,
            transition,
            deps=deps,
        )
    refresh_combo_timeout_watchdog(
        manager,
        deps=deps,
    )
    if (
        decision.consume_current_event
        or decision.passthrough_current_event
        or decision.recall_events
        or decision.action_transition is not None
        or decision.extra_action_transitions
        or decision.reset_candidates
    ):
        return decision
    return None


def emit_combo_recalls(manager: _ComboManager, recall_events: list[ComboSyntheticEvent]) -> None:
    for event in recall_events:
        device = find_grabbed_device_for_binding(manager, event.binding)
        if device is None:
            continue
        is_active = getattr(device, "combo_passthrough_binding_active", None)
        if callable(is_active) and not bool(is_active(event.binding.evdev)):
            continue
        device.emit_combo_release(event.binding.evdev)
        mark_recalled = getattr(device, "mark_combo_recalled_binding", None)
        if callable(mark_recalled):
            mark_recalled(event.binding.evdev)


def find_grabbed_device_for_binding(
    manager: _ComboManager, binding: RuntimeComboBinding
) -> _GrabbedComboDevice | None:
    for device in manager.grabbed_devices.get(binding.hardware_id, []):
        if binding.source and device.interface_id != binding.source:
            continue
        return device
    return None


def held_combo_modifier_bindings_for_scope(
    manager: _ComboManager,
    hardware_id: str,
    source: str,
    *,
    combo_binding_cls: type[RuntimeComboBinding],
) -> set[RuntimeComboBinding]:
    held: set[RuntimeComboBinding] = set()
    for device in manager.grabbed_devices.get(hardware_id, []):
        if source and device.interface_id != source:
            continue
        modifier_getter = getattr(device, "combo_passthrough_held_modifiers", None)
        if not callable(modifier_getter):
            continue
        modifier_names = modifier_getter()
        if not isinstance(modifier_names, (list, tuple, set, frozenset)):
            continue
        modifier_name_values = cast(
            list[object] | tuple[object, ...] | set[object] | frozenset[object],
            modifier_names,
        )
        modifier_names_str = [name for name in modifier_name_values if isinstance(name, str)]
        for evdev_name in modifier_names_str:
            held.add(
                combo_binding_cls(
                    hardware_id=hardware_id,
                    evdev=evdev_name,
                    source=device.interface_id,
                )
            )
    return held


def prime_combo_engine_with_held_bindings(
    manager: _ComboManager,
    *,
    combo_binding_cls: type[RuntimeComboBinding],
) -> None:
    held: set[RuntimeComboBinding] = set()
    for devices in manager.grabbed_devices.values():
        for device in devices:
            held_getter = getattr(device, "combo_held_source_bindings", None)
            if not callable(held_getter):
                continue
            held_names = held_getter()
            if not isinstance(held_names, (list, tuple, set, frozenset)):
                continue
            held_name_values = cast(
                list[object] | tuple[object, ...] | set[object] | frozenset[object],
                held_names,
            )
            held_names_str = [name for name in held_name_values if isinstance(name, str)]
            for evdev_name in held_names_str:
                held.add(
                    combo_binding_cls(
                        hardware_id=str(device.hardware_id or "").lower(),
                        evdev=str(evdev_name or "").lower(),
                        source=str(device.interface_id or "").lower(),
                    )
                )
    manager.combo_state.engine.prime_held_bindings(held)


async def apply_combo_action_transition(
    manager: _ComboManager,
    transition: ComboActionTransition,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    if transition.kind == "press":
        await start_combo_action(
            manager,
            transition.combo_id,
            transition.action,
            transition.trigger_binding,
            transition.trigger_bindings,
            deps=deps,
        )
    elif transition.kind == "release":
        await stop_combo_action(
            manager,
            transition.combo_id,
            deps=deps,
        )


async def broadcast_combo_action(
    manager: _ComboManager,
    data: dict[str, object],
    *,
    deps: ComboRuntimeDeps,
) -> None:
    dispatch_action_trigger(
        manager.broadcast_callback,
        data,
        fire_and_observe_fn=deps.fire_and_observe_fn,
        command_type=CommandType,
        label="combo action broadcast",
    )


def prune_combo_action_task(
    manager: _ComboManager, combo_id: str, task: asyncio.Task[None] | None
) -> None:
    if task is None:
        return
    state = manager.combo_state.active_actions.get(combo_id)
    if state is not None and state.task is task:
        manager.combo_state.active_actions.pop(combo_id, None)


def track_combo_superkey_output(
    manager: _ComboManager,
    action_type: str,
    code: int,
    value: int,
) -> bool:
    bucket = action_type if action_type in manager.combo_state.superkey_output_refcounts else None
    if bucket is None:
        return True

    refcounts = manager.combo_state.superkey_output_refcounts[bucket]
    current = refcounts.get(int(code), 0)

    if int(value) == 1:
        refcounts[int(code)] = current + 1
        manager.combo_state.held_output_keys[bucket].add(int(code))
        return current == 0

    if int(value) == 0:
        if current <= 1:
            # `current == 0` means this release was already balanced elsewhere, so
            # there is no final key-up to emit. Only a 1 -> 0 transition should
            # propagate a release event to the output layer.
            refcounts.pop(int(code), None)
            manager.combo_state.held_output_keys[bucket].discard(int(code))
            return current == 1

        refcounts[int(code)] = current - 1
        return False

    return True


def _combo_step_count(manager: _ComboManager, combo_id: str) -> int:
    combo = _runtime_combo(manager, combo_id)
    if combo is None:
        return 1
    return len(combo.steps)


def _runtime_combo(manager: _ComboManager, combo_id: str) -> RuntimeCombo | None:
    for combo in manager.combo_state.active_combos:
        if combo.id == combo_id:
            return combo
    return None


def _ordered_unique_bindings(
    bindings: Sequence[RuntimeComboBinding],
) -> list[RuntimeComboBinding]:
    ordered: list[RuntimeComboBinding] = []
    seen: set[RuntimeComboBinding] = set()
    for binding in bindings:
        if binding in seen:
            continue
        seen.add(binding)
        ordered.append(binding)
    return ordered


def _combo_superkey_config(
    manager: _ComboManager,
    combo_id: str,
    action: MappingAction,
) -> RuntimeSuperkeyConfig | None:
    config = cast(RuntimeSuperkeyConfig | None, action.superkey_config)
    if config is None:
        return None
    return combo_effective_superkey_config(
        config,
        step_count=_combo_step_count(manager, combo_id),
    )


def _combo_matches_binding_scope(
    combo: RuntimeCombo,
    hardware_id: str,
    source: str | None,
) -> bool:
    """Return whether a combo includes a binding for this already-normalized scope.

    ``hardware_id`` must already use the runtime's canonical casing, and ``source``
    must be pre-normalized the same way or left as ``None`` to match any source.
    This helper only coerces falsey values to ``""`` for comparison.
    """
    normalized_hardware_id = str(hardware_id or "")
    normalized_source = None if source is None else str(source or "")
    for step in combo.steps:
        for binding in step.bindings:
            if binding.hardware_id != normalized_hardware_id:
                continue
            if normalized_source is None or binding.source == normalized_source:
                return True
    return False


async def _combo_superkey_machine(
    manager: _ComboManager,
    combo_id: str,
    action: MappingAction,
    trigger_binding: RuntimeComboBinding,
    *,
    deps: ComboRuntimeDeps,
) -> SuperkeyMachine | None:
    config = _combo_superkey_config(manager, combo_id, action)
    if config is None:
        return None

    trigger_name = f"combo:{combo_id}"
    existing = manager.combo_state.superkey_machines.get(combo_id)
    if existing is not None:
        if existing.config == config and existing.event_name == trigger_name:
            return existing
        await existing.stop()
        manager.combo_state.superkey_machines.pop(combo_id, None)

    async def combo_superkey_broadcast(data: dict[str, object]) -> None:
        payload = dict(data)
        payload.setdefault("source_device", trigger_binding.hardware_id)
        payload.setdefault("source_button", trigger_name)
        await broadcast_combo_action(
            manager,
            payload,
            deps=deps,
        )

    def combo_superkey_output_tracker(action_type: str, code: int, value: int) -> bool:
        return track_combo_superkey_output(manager, action_type, code, value)

    machine = SuperkeyMachine(
        config=config,
        event_name=trigger_name,
        keyboard_uinput=cast(_WritableUInput, manager.output_state.keyboard_uinput),
        mouse_uinput=cast(_WritableUInput, manager.output_state.mouse_uinput),
        gamepad_uinput=cast(_WritableUInput, manager.output_state.gamepad_uinput),
        broadcast_callback=combo_superkey_broadcast,
        cursor_position_setter=manager.set_cursor_position,
        key_event_tracker=combo_superkey_output_tracker,
    )
    manager.combo_state.superkey_machines[combo_id] = machine
    return machine


def _combo_trigger_recall_state(
    manager: _ComboManager,
    combo_id: str,
    trigger_bindings: Sequence[RuntimeComboBinding],
) -> tuple[list[RuntimeComboBinding], list[RuntimeComboBinding]]:
    combo = _runtime_combo(manager, combo_id)
    if combo is None or not combo.recall_trigger_keys:
        return ([], [])

    ordered_bindings = _ordered_unique_bindings(trigger_bindings)
    recalled_bindings: list[RuntimeComboBinding] = []
    for binding in reversed(ordered_bindings):
        device = find_grabbed_device_for_binding(manager, binding)
        is_recalled = getattr(device, "combo_binding_recalled", None)
        if callable(is_recalled) and bool(is_recalled(binding.evdev)):
            recalled_bindings.append(binding)
            continue
        is_active = getattr(device, "combo_passthrough_binding_active", None)
        if callable(is_active) and not bool(is_active(binding.evdev)):
            continue
        if device is not None:
            device.emit_combo_release(binding.evdev)
            mark_recalled = getattr(device, "mark_combo_recalled_binding", None)
            if callable(mark_recalled):
                mark_recalled(binding.evdev)
            recalled_bindings.append(binding)

    restore_names = set(combo.restore_trigger_keys)
    recalled_set = set(recalled_bindings)
    restore_bindings = [
        binding
        for binding in ordered_bindings
        if binding in recalled_set and normalize_combo_evdev(binding.evdev) in restore_names
    ]
    return (recalled_bindings, restore_bindings)


def _restore_combo_trigger_bindings(
    manager: _ComboManager,
    restore_bindings: Sequence[RuntimeComboBinding],
) -> None:
    for binding in restore_bindings:
        device = find_grabbed_device_for_binding(manager, binding)
        if device is None:
            continue
        clear_recalled = getattr(device, "clear_combo_recalled_binding", None)
        is_held = getattr(device, "combo_source_binding_held", None)
        is_active = getattr(device, "combo_passthrough_binding_active", None)
        if callable(is_held) and not bool(is_held(binding.evdev)):
            if callable(clear_recalled):
                clear_recalled(binding.evdev)
            continue
        # Skip restore if passthrough state is already active again. This keeps
        # restore idempotent when the user re-pressed the trigger key during the
        # combo action, or when some other path has already restored it.
        if callable(is_active) and bool(is_active(binding.evdev)):
            if callable(clear_recalled):
                clear_recalled(binding.evdev)
            continue
        emit_press = getattr(device, "emit_combo_press", None)
        if callable(emit_press):
            emit_press(binding.evdev)
        if callable(clear_recalled):
            clear_recalled(binding.evdev)


def _attach_combo_trigger_recall_state(
    manager: _ComboManager,
    combo_id: str,
    recalled_bindings: Sequence[RuntimeComboBinding],
    restore_bindings: Sequence[RuntimeComboBinding],
) -> bool:
    state = manager.combo_state.active_actions.get(combo_id)
    if state is None:
        return False
    state.recalled_bindings = list(recalled_bindings)
    state.restore_bindings = list(restore_bindings)
    return True


async def combo_tap_key(
    manager: _ComboManager,
    combo_id: str,
    uinput_dev: object | None,
    code: int,
    hold_ms: int,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    task = deps.asyncio_mod.current_task()
    pressed = False

    try:
        write_combo_key(uinput_dev, code, 1, deps=deps)
        pressed = True
        await deps.asyncio_mod.sleep(max(0.001, float(hold_ms) / 1000.0))
    except deps.asyncio_mod.CancelledError:
        raise
    finally:
        if pressed:
            write_combo_key(uinput_dev, code, 0, deps=deps)
        prune_combo_action_task(manager, combo_id, task)


async def combo_tap_trigger(
    manager: _ComboManager,
    combo_id: str,
    axis_code: int,
    hold_ms: int,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    task = deps.asyncio_mod.current_task()
    pressed = False

    try:
        write_combo_trigger(
            manager,
            axis_code,
            255,
            deps=deps,
        )
        pressed = True
        await deps.asyncio_mod.sleep(max(0.001, float(hold_ms) / 1000.0))
    except deps.asyncio_mod.CancelledError:
        raise
    finally:
        if pressed:
            write_combo_trigger(
                manager,
                axis_code,
                0,
                deps=deps,
            )
        prune_combo_action_task(manager, combo_id, task)


async def start_combo_action(
    manager: _ComboManager,
    combo_id: str,
    action: MappingAction | None,
    trigger_binding: RuntimeComboBinding,
    trigger_bindings: Sequence[RuntimeComboBinding],
    *,
    deps: ComboRuntimeDeps,
) -> None:
    if action is None:
        return

    await stop_combo_action(
        manager,
        combo_id,
        deps=deps,
    )
    trigger_name = f"combo:{combo_id}"
    recalled_bindings, restore_bindings = _combo_trigger_recall_state(
        manager,
        combo_id,
        trigger_bindings,
    )

    if action.action_type == ActionType.SUPERKEY:
        config = cast(RuntimeSuperkeyConfig | None, action.superkey_config)
        if config is None:
            return
        if config.mode == SuperkeyMode.OVERLOAD:
            child_combo_ids: list[str] = []
            for index, child_action in enumerate(config.overload_actions):
                if child_action.action_type == ActionType.SUPERKEY:
                    # Combo-triggered overloads intentionally stop at one superkey layer
                    # so a saved superkey cannot recursively expand into more superkeys.
                    log.warning(
                        "Skipping nested superkey child %s in combo overload %s (%s)",
                        child_action.superkey_name or "<unnamed>",
                        combo_id,
                        config.name,
                    )
                    continue
                child_combo_id = f"{combo_id}#overload#{index}"
                await _start_combo_action_instance(
                    manager,
                    child_combo_id,
                    child_action,
                    trigger_binding,
                    trigger_name=f"{trigger_name}#overload#{index}",
                    deps=deps,
                )
                if child_combo_id in manager.combo_state.active_actions:
                    child_combo_ids.append(child_combo_id)
            manager.combo_state.active_actions[combo_id] = ComboActionState(
                kind="superkey_overload",
                child_combo_ids=child_combo_ids,
                recalled_bindings=recalled_bindings,
                restore_bindings=restore_bindings,
            )
            return

        machine = await _combo_superkey_machine(
            manager,
            combo_id,
            action,
            trigger_binding,
            deps=deps,
        )
        if machine is None:
            return
        manager.combo_state.active_actions[combo_id] = ComboActionState(
            kind="superkey_pattern",
            machine=machine,
            recalled_bindings=recalled_bindings,
            restore_bindings=restore_bindings,
        )
        await machine.on_down()
        return

    await _start_combo_action_instance(
        manager,
        combo_id,
        action,
        trigger_binding,
        trigger_name=trigger_name,
        deps=deps,
    )
    if not _attach_combo_trigger_recall_state(
        manager,
        combo_id,
        recalled_bindings,
        restore_bindings,
    ):
        _restore_combo_trigger_bindings(manager, restore_bindings)


async def _start_combo_action_instance(
    manager: _ComboManager,
    combo_id: str,
    action: MappingAction | None,
    trigger_binding: RuntimeComboBinding,
    *,
    trigger_name: str,
    deps: ComboRuntimeDeps,
) -> None:
    if action is None or action.action_type == ActionType.SUPERKEY:
        return

    if action.action_type == ActionType.KEYBOARD and action.target:
        await start_combo_key_action(
            manager,
            combo_id,
            action,
            manager.output_state.keyboard_uinput,
            deps=deps,
        )
        return

    if action.action_type == ActionType.MOUSE and action.target:
        await start_combo_mouse_action(
            manager,
            combo_id,
            action,
            manager.output_state.mouse_uinput,
            deps=deps,
        )
        return

    if action.action_type == ActionType.GAMEPAD and action.target:
        is_trigger, axis_code = deps.get_trigger_axis_fn(action.target)
        if is_trigger and axis_code is not None:
            if action.tap_enabled:
                task = deps.asyncio_mod.create_task(
                    combo_tap_trigger(
                        manager,
                        combo_id,
                        axis_code,
                        action.tap_hold_ms,
                        deps=deps,
                    )
                )
                manager.combo_state.active_actions[combo_id] = ComboActionState(
                    kind="tap_trigger",
                    axis_code=axis_code,
                    task=task,
                )
                return
            if action.rapidfire_enabled:
                task = deps.asyncio_mod.create_task(
                    combo_rapidfire_trigger(
                        manager,
                        combo_id,
                        axis_code,
                        action.rapidfire_hold_ms,
                        action.rapidfire_wait_ms,
                        deps=deps,
                    )
                )
                manager.combo_state.active_actions[combo_id] = ComboActionState(
                    kind="rapidfire_trigger",
                    axis_code=axis_code,
                    active=True,
                    task=task,
                )
                return
            write_combo_trigger(
                manager,
                axis_code,
                255,
                deps=deps,
            )
            manager.combo_state.active_actions[combo_id] = ComboActionState(
                kind="trigger",
                axis_code=axis_code,
            )
            return
        await start_combo_key_action(
            manager,
            combo_id,
            action,
            manager.output_state.gamepad_uinput,
            deps=deps,
        )
        return

    if action.action_type == ActionType.MOUSE_MOVE_ABS:
        await manager.set_cursor_position(int(action.move_x), int(action.move_y))
        return

    if action.action_type == ActionType.MOUSE_MOVE_REL:
        emit_combo_mouse_move(manager, action, deps=deps)
        return

    if action.action_type == ActionType.MACRO:
        macro_request = build_macro_playback_request(
            action,
            source_device="combo",
            source_button=trigger_name,
            trigger_value=1,
        )
        if macro_request is not None:
            await manager.play_macro(**macro_request)
            if is_hold_macro_action(action):
                manager.combo_state.active_actions[combo_id] = ComboActionState(
                    kind="macro_hold",
                    action=action,
                    source_device="combo",
                    source_button=trigger_name,
                )
        return

    action_payload = build_action_trigger_payload(
        action,
        source_device=trigger_binding.hardware_id,
        source_button=trigger_name,
    )
    if action_payload is not None:
        await broadcast_combo_action(
            manager,
            action_payload,
            deps=deps,
        )
        return


async def start_combo_key_action(
    manager: _ComboManager,
    combo_id: str,
    action: MappingAction,
    uinput_dev: object | None,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    target = str(action.target or "")
    if not target:
        return
    code = deps.resolve_code_fn(target)
    if code is None:
        return
    if action.tap_enabled:
        task = deps.asyncio_mod.create_task(
            combo_tap_key(
                manager,
                combo_id,
                uinput_dev,
                code,
                action.tap_hold_ms,
                deps=deps,
            )
        )
        manager.combo_state.active_actions[combo_id] = ComboActionState(
            kind="tap_key",
            uinput=uinput_dev,
            code=code,
            task=task,
        )
        return
    if action.rapidfire_enabled:
        task = deps.asyncio_mod.create_task(
            combo_rapidfire_key(
                manager,
                combo_id,
                uinput_dev,
                code,
                action.rapidfire_hold_ms,
                action.rapidfire_wait_ms,
                deps=deps,
            )
        )
        manager.combo_state.active_actions[combo_id] = ComboActionState(
            kind="rapidfire_key",
            uinput=uinput_dev,
            code=code,
            active=True,
            task=task,
        )
        return
    write_combo_key(uinput_dev, code, 1, deps=deps)
    manager.combo_state.active_actions[combo_id] = ComboActionState(
        kind="key",
        uinput=uinput_dev,
        code=code,
    )


async def start_combo_mouse_action(
    manager: _ComboManager,
    combo_id: str,
    action: MappingAction,
    uinput_dev: object | None,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    target = resolve_mouse_output_target(action.target)
    if target is None:
        return
    if not target.is_relative:
        await start_combo_key_action(
            manager,
            combo_id,
            action,
            uinput_dev,
            deps=deps,
        )
        return
    if action.tap_enabled:
        task = deps.asyncio_mod.create_task(
            combo_tap_relative(
                manager,
                combo_id,
                uinput_dev,
                target.code,
                target.relative_value,
                action.tap_hold_ms,
                deps=deps,
            )
        )
        manager.combo_state.active_actions[combo_id] = ComboActionState(
            kind="tap_relative",
            uinput=uinput_dev,
            code=target.code,
            task=task,
        )
        return
    if action.rapidfire_enabled:
        task = deps.asyncio_mod.create_task(
            combo_rapidfire_relative(
                manager,
                combo_id,
                uinput_dev,
                target.code,
                target.relative_value,
                action.rapidfire_hold_ms,
                action.rapidfire_wait_ms,
                deps=deps,
            )
        )
        manager.combo_state.active_actions[combo_id] = ComboActionState(
            kind="rapidfire_relative",
            uinput=uinput_dev,
            code=target.code,
            active=True,
            task=task,
        )
        return
    write_combo_relative(
        uinput_dev,
        target.code,
        target.relative_value,
        deps=deps,
    )


async def stop_combo_action(
    manager: _ComboManager,
    combo_id: str,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    state = manager.combo_state.active_actions.pop(combo_id, None)
    if not state:
        return
    kind = state.kind
    restore_bindings = state.restore_bindings
    if kind == "superkey_overload":
        for child_combo_id in reversed(state.child_combo_ids):
            await stop_combo_action(
                manager,
                child_combo_id,
                deps=deps,
            )
        _restore_combo_trigger_bindings(manager, restore_bindings)
        return
    if kind == "superkey_pattern":
        machine = state.machine
        if machine is not None:
            await machine.on_up()
        _restore_combo_trigger_bindings(manager, restore_bindings)
        return
    if kind == "key":
        uinput_dev = state.uinput
        code = state.code
        if code is not None:
            write_combo_key(
                uinput_dev,
                code,
                0,
                deps=deps,
            )
        _restore_combo_trigger_bindings(manager, restore_bindings)
        return
    if kind == "trigger":
        axis_code = state.axis_code
        if axis_code is not None:
            write_combo_trigger(
                manager,
                axis_code,
                0,
                deps=deps,
            )
        _restore_combo_trigger_bindings(manager, restore_bindings)
        return
    if kind in {
        "tap_key",
        "tap_trigger",
        "rapidfire_key",
        "rapidfire_trigger",
        "tap_relative",
        "rapidfire_relative",
    }:
        state.active = False
        task = state.task
        if task is not None and not task.done():
            task.cancel()
            with deps.contextlib_mod.suppress(deps.asyncio_mod.CancelledError):
                await task
        _restore_combo_trigger_bindings(manager, restore_bindings)
        return
    if kind == "macro_hold":
        action = state.action
        if action is not None:
            macro_request = build_macro_playback_request(
                action,
                source_device=str(state.source_device or ""),
                source_button=str(state.source_button or ""),
                trigger_value=0,
                include_macro_events=False,
            )
            if macro_request is not None:
                await manager.play_macro(**macro_request)
        _restore_combo_trigger_bindings(manager, restore_bindings)
        return
    _restore_combo_trigger_bindings(manager, restore_bindings)


async def clear_combo_runtime(
    manager: _ComboManager,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    manager.combo_state.engine.reset()
    for combo_id in list(manager.combo_state.active_actions):
        await stop_combo_action(
            manager,
            combo_id,
            deps=deps,
        )
    # stop_combo_action() handles the pattern key-up transition for active combo
    # actions. This final pass is still required to fully tear down any cached
    # machine state and cancel timers during combo runtime reset.
    machines = list(manager.combo_state.superkey_machines.values())
    manager.combo_state.superkey_machines.clear()
    for machine in machines:
        await machine.stop()
    for held in manager.combo_state.held_output_keys.values():
        held.clear()
    for refcounts in manager.combo_state.superkey_output_refcounts.values():
        refcounts.clear()
    if manager.combo_state.timeout_task and not manager.combo_state.timeout_task.done():
        manager.combo_state.timeout_task.cancel()
        with deps.contextlib_mod.suppress(deps.asyncio_mod.CancelledError):
            await manager.combo_state.timeout_task
    manager.combo_state.timeout_task = None


async def clear_combo_runtime_for_binding_scope(
    manager: _ComboManager,
    hardware_id: str,
    source: str | None,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    normalized_hardware_id = str(hardware_id or "").lower()
    normalized_source = None if source is None else str(source or "").lower()
    active_combo_ids = manager.combo_state.engine.drop_candidates_for_binding_scope(
        normalized_hardware_id,
        normalized_source,
    )
    for combo_id in active_combo_ids:
        await stop_combo_action(
            manager,
            combo_id,
            deps=deps,
        )
    matching_machine_ids = [
        combo.id
        for combo in manager.combo_state.active_combos
        if combo.id in manager.combo_state.superkey_machines
        and _combo_matches_binding_scope(combo, normalized_hardware_id, normalized_source)
    ]
    for combo_id in matching_machine_ids:
        machine = manager.combo_state.superkey_machines.pop(combo_id, None)
        if machine is not None:
            await machine.stop()
    refresh_combo_timeout_watchdog(
        manager,
        deps=deps,
    )


def refresh_combo_timeout_watchdog(
    manager: _ComboManager,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    deadline = manager.combo_state.engine.next_deadline()
    if deadline is None:
        if manager.combo_state.timeout_task and not manager.combo_state.timeout_task.done():
            manager.combo_state.timeout_task.cancel()
        manager.combo_state.timeout_task = None
        return
    if manager.combo_state.timeout_task and not manager.combo_state.timeout_task.done():
        manager.combo_state.timeout_task.cancel()
    manager.combo_state.timeout_task = deps.asyncio_mod.create_task(
        combo_timeout_watchdog(
            manager,
            deadline,
            deps=deps,
        )
    )


async def combo_timeout_watchdog(
    manager: _ComboManager,
    deadline: float,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    try:
        await deps.asyncio_mod.sleep(max(0.0, deadline - deps.time_mod.monotonic()))
        manager.combo_state.engine.expire_timeouts(deps.time_mod.monotonic())
    except deps.asyncio_mod.CancelledError:
        raise
    finally:
        if manager.combo_state.timeout_task is deps.asyncio_mod.current_task():
            manager.combo_state.timeout_task = None
        refresh_combo_timeout_watchdog(
            manager,
            deps=deps,
        )


async def combo_rapidfire_key(
    manager: _ComboManager,
    combo_id: str,
    uinput_dev: object | None,
    code: int,
    hold_ms: int,
    wait_ms: int,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    try:
        while _combo_action_active(manager, combo_id):
            write_combo_key(uinput_dev, code, 1, deps=deps)
            await deps.asyncio_mod.sleep(max(0.001, hold_ms / 1000.0))
            if not _combo_action_active(manager, combo_id):
                break
            write_combo_key(uinput_dev, code, 0, deps=deps)
            await deps.asyncio_mod.sleep(max(0.001, wait_ms / 1000.0))
    except deps.asyncio_mod.CancelledError:
        raise
    finally:
        write_combo_key(uinput_dev, code, 0, deps=deps)


async def combo_tap_relative(
    manager: _ComboManager,
    combo_id: str,
    uinput_dev: object | None,
    code: int,
    value: int,
    hold_ms: int,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    task = deps.asyncio_mod.current_task()

    try:
        await tap_relative_pulse(
            emit_pulse=lambda: write_combo_relative(
                uinput_dev,
                code,
                value,
                deps=deps,
            ),
            hold_s=max(0.001, float(hold_ms) / 1000.0),
            asyncio_mod=deps.asyncio_mod,
        )
    except deps.asyncio_mod.CancelledError:
        raise
    finally:
        prune_combo_action_task(manager, combo_id, task)


async def combo_rapidfire_relative(
    manager: _ComboManager,
    combo_id: str,
    uinput_dev: object | None,
    code: int,
    value: int,
    hold_ms: int,
    wait_ms: int,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    try:
        await rapidfire_relative_pulses(
            emit_pulse=lambda: write_combo_relative(
                uinput_dev,
                code,
                value,
                deps=deps,
            ),
            is_active=lambda: _combo_action_active(manager, combo_id),
            hold_s=max(0.001, hold_ms / 1000.0),
            wait_s=max(0.001, wait_ms / 1000.0),
            asyncio_mod=deps.asyncio_mod,
        )
    except deps.asyncio_mod.CancelledError:
        raise


async def combo_rapidfire_trigger(
    manager: _ComboManager,
    combo_id: str,
    axis_code: int,
    hold_ms: int,
    wait_ms: int,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    try:
        while _combo_action_active(manager, combo_id):
            write_combo_trigger(
                manager,
                axis_code,
                255,
                deps=deps,
            )
            await deps.asyncio_mod.sleep(max(0.001, hold_ms / 1000.0))
            if not _combo_action_active(manager, combo_id):
                break
            write_combo_trigger(
                manager,
                axis_code,
                0,
                deps=deps,
            )
            await deps.asyncio_mod.sleep(max(0.001, wait_ms / 1000.0))
    except deps.asyncio_mod.CancelledError:
        raise
    finally:
        write_combo_trigger(manager, axis_code, 0, deps=deps)


def write_combo_key(
    uinput_dev: object | None,
    code: int,
    value: int,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    writer = deps.uinput_writer(uinput_dev)
    if writer is None:
        return
    writer.write(deps.evdev_mod.ecodes.EV_KEY, int(code), int(value))
    writer.syn()


def write_combo_relative(
    uinput_dev: object | None,
    code: int,
    value: int,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    write_relative_pulse(
        uinput_dev,
        code,
        value,
        ev_rel_code=deps.evdev_mod.ecodes.EV_REL,
        uinput_writer=deps.uinput_writer,
    )


def write_combo_trigger(
    manager: _ComboManager,
    axis_code: int,
    value: int,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    writer = deps.uinput_writer(manager.output_state.gamepad_uinput)
    if writer is None:
        return
    writer.write(deps.evdev_mod.ecodes.EV_ABS, int(axis_code), int(value))
    writer.syn()


def emit_combo_mouse_move(
    manager: _ComboManager, action: MappingAction, *, deps: ComboRuntimeDeps
) -> None:
    deps.emit_mouse_move_fn(
        manager.output_state.mouse_uinput,
        int(action.move_x),
        int(action.move_y),
        absolute=bool(action.action_type.value == "mouse_move_abs"),
    )


def _combo_action_active(manager: _ComboManager, combo_id: str) -> bool:
    state = manager.combo_state.active_actions.get(combo_id)
    return state.active if state is not None else False


def begin_combo_capture(
    manager: _ComboManager,
    token: str,
    hardware_ids: set[str],
    notify_event: asyncio.Event | None,
    *,
    queue_mod: _QueueModule,
) -> dict[str, object]:
    manager.combo_state.capture_queues[token] = (
        queue_mod.SimpleQueue(),
        set(hardware_ids),
        notify_event,
    )
    return {
        "token": token,
        "grabbed_devices": sum(len(devices) for devices in manager.grabbed_devices.values()),
    }


def read_combo_capture(
    manager: _ComboManager, token: str, *, queue_mod: _QueueModule
) -> dict[str, object]:
    capture_state = manager.combo_state.capture_queues.get(token)
    if capture_state is None:
        return {"event": None}
    capture_queue, _hardware_ids, _notify_event = capture_state
    try:
        return {"event": capture_queue.get_nowait()}
    except queue_mod.Empty:
        return {"event": None}


def end_combo_capture(manager: _ComboManager, token: str) -> dict[str, object]:
    removed = manager.combo_state.capture_queues.pop(token, None)
    return {"status": "ok", "ended": removed is not None}
