"""Combo action execution, including superkey and repeat integration."""

import asyncio
import logging
from collections.abc import Sequence
from typing import cast

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType, SuperkeyMode
from keymasq.keymasqd.combo_engine import (
    ComboActionTransition,
    RuntimeComboBinding,
)
from keymasq.keymasqd.runtime import action_runner
from keymasq.keymasqd.runtime.action.state import (
    ActionExecutionHandle,
    CancelMacroPlayback,
    cancel_action_tasks,
    drain_action_tasks,
)
from keymasq.keymasqd.runtime.action.triggers import (
    dispatch_action_trigger,
    is_hold_macro_action,
    source_trigger_id,
)
from keymasq.keymasqd.runtime.combo import execution, superkeys
from keymasq.keymasqd.runtime.combo.recall import (
    attach_combo_trigger_recall_state,
    combo_trigger_recall_state,
    ordered_unique_bindings,
    restore_combo_trigger_bindings,
)
from keymasq.keymasqd.runtime.combo.state import (
    ComboActionState,
    ComboManager,
    ComboRuntimeDeps,
    ResolveCodeFn,
)
from keymasq.keymasqd.runtime.grabbed_device.types import ActionExecutionDeps, ActionRuntime
from keymasq.keymasqd.runtime.repeat import (
    SUPERKEY_SLOT_OVERLOAD,
    RepeatHistoryEntry,
    execute_repeated_superkey_path,
    remember_superkey_path,
)
from keymasq.keymasqd.superkey_state import SuperkeyConfig, SuperkeyMachine

log = logging.getLogger("keymasqd.runtime.combo_actions")


async def apply_combo_action_transition(
    manager: ComboManager,
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
    elif transition.kind == "pulse":
        await start_combo_action(
            manager,
            transition.combo_id,
            transition.action,
            transition.trigger_binding,
            transition.trigger_bindings,
            deps=deps,
        )
        await wait_combo_action_started(manager, transition.combo_id)
        await stop_combo_action(
            manager,
            transition.combo_id,
            deps=deps,
        )
    elif transition.kind == "release":
        await stop_combo_action(
            manager,
            transition.combo_id,
            deps=deps,
        )


async def broadcast_combo_action(
    manager: ComboManager,
    data: dict[str, object],
    *,
    deps: ComboRuntimeDeps,
) -> None:
    dispatch_action_trigger(
        manager.broadcast_callback,
        data,
        fire_and_observe_fn=deps.fire_and_observe_fn,
        label="combo action broadcast",
    )


def _observe_combo_profile_trigger(
    manager: ComboManager,
    trigger_binding: RuntimeComboBinding,
    trigger_name: str,
    *,
    active: bool,
) -> None:
    observer = (
        getattr(manager, "observe_profile_trigger_start", None)
        if active
        else getattr(manager, "observe_profile_trigger_end", None)
    )
    if callable(observer):
        observer(source_trigger_id(trigger_binding.hardware_id, trigger_name))


async def wait_combo_action_started(manager: ComboManager, combo_id: str) -> None:
    state = manager.combo_state.active_actions.get(combo_id)
    if state is None:
        return
    if state.kind in {"superkey_overload", "superkey_overload_split"}:
        for child_combo_id in state.child_combo_ids:
            await wait_combo_action_started(manager, child_combo_id)
        return
    if state.started is None:
        return
    await state.started.wait()


def track_combo_superkey_output(
    manager: ComboManager,
    action_type: str,
    code: int,
    value: int,
) -> bool:
    return superkeys.track_output(manager, action_type, code, value)


async def _combo_superkey_machine(
    manager: ComboManager,
    combo_id: str,
    action: MappingAction,
    trigger_binding: RuntimeComboBinding,
    trigger_bindings: Sequence[RuntimeComboBinding],
    *,
    deps: ComboRuntimeDeps,
) -> SuperkeyMachine | None:
    return await superkeys.build_machine(
        manager,
        combo_id,
        action,
        trigger_binding,
        trigger_bindings,
        deps=deps,
        machine_type=SuperkeyMachine,
    )


async def start_combo_action(
    manager: ComboManager,
    combo_id: str,
    action: MappingAction | None,
    trigger_binding: RuntimeComboBinding,
    trigger_bindings: Sequence[RuntimeComboBinding],
    *,
    deps: ComboRuntimeDeps,
    record_repeat: bool = True,
) -> None:
    if action is None:
        return

    await stop_combo_action(
        manager,
        combo_id,
        deps=deps,
    )
    trigger_name = f"combo:{combo_id}"

    superkey_config: SuperkeyConfig | None = None
    superkey_machine: SuperkeyMachine | None = None
    if action.action_type == ActionType.SUPERKEY:
        superkey_config = cast(SuperkeyConfig | None, action.superkey_config)
        if superkey_config is None:
            return
        if superkey_config.mode != SuperkeyMode.OVERLOAD:
            superkey_machine = await _combo_superkey_machine(
                manager,
                combo_id,
                action,
                trigger_binding,
                trigger_bindings,
                deps=deps,
            )
            if superkey_machine is None:
                return

    _observe_combo_profile_trigger(
        manager,
        trigger_binding,
        trigger_name,
        active=True,
    )
    recorder = getattr(manager, "record_profile_action", None)
    if callable(recorder):
        recorder(action.source_profile_name)
    recalled_bindings, restore_bindings = combo_trigger_recall_state(
        manager,
        combo_id,
        trigger_bindings,
    )

    if action.action_type == ActionType.SUPERKEY:
        config = cast(SuperkeyConfig, superkey_config)
        if config.mode == SuperkeyMode.OVERLOAD:
            await _start_combo_overload_superkey(
                manager,
                combo_id,
                action,
                config,
                trigger_binding,
                trigger_name,
                recalled_bindings,
                restore_bindings,
                deps=deps,
                record_repeat=record_repeat,
            )
            return

        machine = cast(SuperkeyMachine, superkey_machine)
        manager.combo_state.active_actions[combo_id] = ComboActionState(
            kind="superkey_pattern",
            machine=machine,
            trigger_binding=trigger_binding,
            trigger_bindings=list(ordered_unique_bindings(trigger_bindings)),
            source_button=trigger_name,
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
    state = manager.combo_state.active_actions.get(combo_id)
    if state is not None:
        state.trigger_binding = trigger_binding
        state.source_button = trigger_name
    else:
        _observe_combo_profile_trigger(
            manager,
            trigger_binding,
            trigger_name,
            active=False,
        )
    if not attach_combo_trigger_recall_state(
        manager,
        combo_id,
        recalled_bindings,
        restore_bindings,
    ):
        restore_combo_trigger_bindings(manager, restore_bindings)


async def _start_combo_overload_superkey(
    manager: ComboManager,
    combo_id: str,
    action: MappingAction,
    config: SuperkeyConfig,
    trigger_binding: RuntimeComboBinding,
    trigger_name: str,
    recalled_bindings: Sequence[RuntimeComboBinding],
    restore_bindings: Sequence[RuntimeComboBinding],
    *,
    deps: ComboRuntimeDeps,
    record_repeat: bool,
) -> None:
    if record_repeat:
        remember_superkey_path(
            manager.repeat_state,
            action,
            SUPERKEY_SLOT_OVERLOAD,
            source_device=trigger_binding.hardware_id,
            source_button=trigger_name,
        )
    if config.overload_down_actions or config.overload_up_actions:
        split_child_combo_ids: list[str] = []
        for index, child_action in enumerate(config.overload_actions):
            if child_action.action_type == ActionType.SUPERKEY:
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
                record_repeat=False,
            )
            if child_combo_id in manager.combo_state.active_actions:
                split_child_combo_ids.append(child_combo_id)
        for index, child_action in enumerate(config.overload_down_actions):
            if child_action.action_type == ActionType.SUPERKEY:
                log.warning(
                    "Skipping nested superkey child %s in combo overload %s (%s)",
                    child_action.superkey_name or "<unnamed>",
                    combo_id,
                    config.name,
                )
                continue
            child_combo_id = f"{combo_id}#overload_down#{index}"
            await _pulse_combo_action_instance(
                manager,
                child_combo_id,
                child_action,
                trigger_binding,
                trigger_name=f"{trigger_name}#overload_down#{index}",
                deps=deps,
                record_repeat=False,
            )
        manager.combo_state.active_actions[combo_id] = ComboActionState(
            kind="superkey_overload_split",
            child_combo_ids=split_child_combo_ids,
            action=action,
            trigger_binding=trigger_binding,
            source_button=trigger_name,
            recalled_bindings=list(recalled_bindings),
            restore_bindings=list(restore_bindings),
        )
        return

    child_combo_ids: list[str] = []
    for index, child_action in enumerate(config.overload_actions):
        if child_action.action_type == ActionType.SUPERKEY:
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
            record_repeat=False,
        )
        if child_combo_id in manager.combo_state.active_actions:
            child_combo_ids.append(child_combo_id)
    manager.combo_state.active_actions[combo_id] = ComboActionState(
        kind="superkey_overload",
        child_combo_ids=child_combo_ids,
        trigger_binding=trigger_binding,
        source_button=trigger_name,
        recalled_bindings=list(recalled_bindings),
        restore_bindings=list(restore_bindings),
    )


async def _start_combo_superkey_repeat_path(
    manager: ComboManager,
    combo_id: str,
    repeated_entry: RepeatHistoryEntry,
    trigger_binding: RuntimeComboBinding,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    async def execute_overload_once(action: MappingAction, repeat_event_name: str) -> None:
        config = cast(SuperkeyConfig, action.superkey_config)
        await _start_combo_overload_superkey(
            manager,
            combo_id,
            action,
            config,
            trigger_binding,
            repeat_event_name,
            (),
            (),
            deps=deps,
            record_repeat=False,
        )
        await stop_combo_action(manager, combo_id, deps=deps)

    async def execute_pattern_slot_once(
        action: MappingAction,
        slot: str,
        _repeat_event_name: str,
    ) -> None:
        machine = await _combo_superkey_machine(
            manager,
            combo_id,
            action,
            trigger_binding,
            [trigger_binding],
            deps=deps,
        )
        if machine is None:
            return
        await machine.execute_repeat_slot(slot)
        await machine.stop()
        manager.combo_state.superkey_machines.pop(combo_id, None)
        manager.combo_state.superkey_machine_bindings.pop(combo_id, None)

    await execute_repeated_superkey_path(
        repeated_entry,
        f"combo:{combo_id}",
        execute_overload_once=execute_overload_once,
        execute_pattern_slot_once=execute_pattern_slot_once,
    )


async def _start_combo_action_instance(
    manager: ComboManager,
    combo_id: str,
    action: MappingAction | None,
    trigger_binding: RuntimeComboBinding,
    *,
    trigger_name: str,
    deps: ComboRuntimeDeps,
    record_repeat: bool = True,
) -> None:
    if action is None or action.action_type == ActionType.SUPERKEY:
        return

    if action.action_type in {ActionType.PASSTHROUGH, ActionType.SUPPRESS}:
        return

    runtime = execution.action_runtime(
        manager,
        combo_id,
        action,
        trigger_binding,
        trigger_name=trigger_name,
    )
    if execution.profile_action_tracks_trigger(action):
        _observe_combo_profile_trigger(
            manager,
            trigger_binding,
            trigger_name,
            active=True,
        )
    started = deps.asyncio_mod.create_event()
    handle = ActionExecutionHandle(started=started)
    combo_deps = deps

    async def repeat_superkey_executor(
        device_runtime: ActionRuntime,
        repeated_entry: RepeatHistoryEntry,
        event_name: str,
        *,
        deps: ActionExecutionDeps,
        execution_handle: ActionExecutionHandle | None = None,
        cancel_macro_playback: CancelMacroPlayback | None = None,
        resolve_code_fn: ResolveCodeFn | None = None,
    ) -> None:
        del device_runtime, deps, execution_handle, cancel_macro_playback, resolve_code_fn
        repeat_combo_id = event_name.removeprefix("combo:")
        await _start_combo_superkey_repeat_path(
            manager,
            repeat_combo_id,
            repeated_entry,
            trigger_binding,
            deps=combo_deps,
        )

    needs_release = execution.action_needs_release(action)
    preregistered = False
    if needs_release and action.action_type != ActionType.REPEAT:
        manager.combo_state.active_actions[combo_id] = ComboActionState(
            kind="executor",
            action=action,
            trigger_binding=trigger_binding,
            source_device=runtime.hardware_id,
            source_button=trigger_name,
            started=handle.started,
            action_runtime=runtime,
            execution_handle=handle,
        )
        preregistered = True

    try:
        await action_runner.execute_action(
            runtime,
            action,
            execution.synthetic_event(trigger_binding, 1),
            trigger_name,
            deps=execution.action_execution_deps(deps),
            execution_handle=handle,
            cancel_macro_playback=manager.cancel_macro_playback,
            repeat_superkey_executor=repeat_superkey_executor,
            resolve_code_fn=deps.resolve_code_fn,
            record_repeat=record_repeat,
        )
        await started.wait()
    except asyncio.CancelledError:
        if preregistered:
            manager.combo_state.active_actions.pop(combo_id, None)
            runtime.stop()
        raise
    except Exception:
        if preregistered:
            manager.combo_state.active_actions.pop(combo_id, None)
            runtime.stop()
        raise

    if is_hold_macro_action(action):
        await drain_action_tasks(handle)

    if action.action_type == ActionType.REPEAT and not runtime.state.repeat_active_actions:
        needs_release = False
    if needs_release:
        if preregistered:
            state = manager.combo_state.active_actions.get(combo_id)
            if state is None:
                runtime.stop()
                return
            state.trigger_binding = trigger_binding
            state.source_device = runtime.hardware_id
            state.source_button = trigger_name
            return
        manager.combo_state.active_actions[combo_id] = ComboActionState(
            kind="executor",
            action=action,
            trigger_binding=trigger_binding,
            source_device=runtime.hardware_id,
            source_button=trigger_name,
            started=handle.started,
            action_runtime=runtime,
            execution_handle=handle,
        )
        return

    # Emergency reset tears down combo state itself. Its observed task must run
    # after the current combo transition releases the ordering lock.
    if action.action_type != ActionType.EMERGENCY_RESET:
        await drain_action_tasks(handle)


async def _pulse_combo_action_instance(
    manager: ComboManager,
    combo_id: str,
    action: MappingAction | None,
    trigger_binding: RuntimeComboBinding,
    *,
    trigger_name: str,
    deps: ComboRuntimeDeps,
    record_repeat: bool = True,
) -> None:
    await _start_combo_action_instance(
        manager,
        combo_id,
        action,
        trigger_binding,
        trigger_name=trigger_name,
        deps=deps,
        record_repeat=record_repeat,
    )
    await wait_combo_action_started(manager, combo_id)
    await stop_combo_action(manager, combo_id, deps=deps)


async def stop_combo_action(
    manager: ComboManager,
    combo_id: str,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    state = manager.combo_state.active_actions.pop(combo_id, None)
    if not state:
        return
    trigger_binding = state.trigger_binding
    source_button = str(state.source_button or f"combo:{combo_id}")
    if trigger_binding is not None:
        _observe_combo_profile_trigger(
            manager,
            trigger_binding,
            source_button,
            active=False,
        )
    kind = state.kind
    restore_bindings = state.restore_bindings
    if kind == "superkey_overload":
        for child_combo_id in reversed(state.child_combo_ids):
            await stop_combo_action(
                manager,
                child_combo_id,
                deps=deps,
            )
        restore_combo_trigger_bindings(manager, restore_bindings)
        return
    if kind == "superkey_overload_split":
        action = state.action
        trigger_binding = state.trigger_binding
        config = cast(SuperkeyConfig | None, action.superkey_config) if action else None
        if trigger_binding is not None and config is not None:
            trigger_name = str(state.source_button or f"combo:{combo_id}")
            for index, child_action in enumerate(config.overload_up_actions):
                if child_action.action_type == ActionType.SUPERKEY:
                    log.warning(
                        "Skipping nested superkey child %s in combo overload %s (%s)",
                        child_action.superkey_name or "<unnamed>",
                        combo_id,
                        config.name,
                    )
                    continue
                await _pulse_combo_action_instance(
                    manager,
                    f"{combo_id}#overload_up#{index}",
                    child_action,
                    trigger_binding,
                    trigger_name=f"{trigger_name}#overload_up#{index}",
                    deps=deps,
                    record_repeat=False,
                )
        for child_combo_id in reversed(state.child_combo_ids):
            await stop_combo_action(
                manager,
                child_combo_id,
                deps=deps,
            )
        restore_combo_trigger_bindings(manager, restore_bindings)
        return
    if kind == "superkey_pattern":
        machine = state.machine
        if machine is not None:
            await machine.on_up()
        restore_combo_trigger_bindings(manager, restore_bindings)
        return
    if kind == "executor":
        runtime = state.action_runtime
        action = state.action
        handle = state.execution_handle
        if runtime is not None and action is not None:
            await action_runner.execute_action(
                runtime,
                action,
                execution.synthetic_event(trigger_binding, 0),
                source_button,
                deps=execution.action_execution_deps(deps),
                execution_handle=handle,
                cancel_macro_playback=manager.cancel_macro_playback,
                resolve_code_fn=deps.resolve_code_fn,
            )
            if execution.uses_tap_task(action):
                await cancel_action_tasks(handle)
            else:
                await drain_action_tasks(handle)
            runtime.stop()
        restore_combo_trigger_bindings(manager, restore_bindings)
        return
    restore_combo_trigger_bindings(manager, restore_bindings)
