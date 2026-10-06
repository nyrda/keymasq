"""Rollover group stage between combo routing and mapped action dispatch.

Member key events update their group's held stack. When the active member
changes, the old member is released and the new one is pressed through the
normal mapped action path, using synthetic events, whatever the member's action
is. Plain members that write the same output hand over without the release, so
an axis moves straight from one value to the next.
"""

import asyncio
import bisect
from collections.abc import Coroutine

from keymasq.common.model.core import ActionType
from keymasq.common.model.profiles import RolloverMember
from keymasq.common.rollover import ONE_SHOT_ACTION_TYPES
from keymasq.common.types import SyntheticInputEvent
from keymasq.keymasqd.output_helpers import resolve_output_code
from keymasq.keymasqd.runtime.adapters import identity_uinput_writer
from keymasq.keymasqd.runtime.grabbed_device import actions, outputs
from keymasq.keymasqd.runtime.grabbed_device.event.classification import (
    find_action_for_code,
    normalize_evdev_binding_value,
)
from keymasq.keymasqd.runtime.grabbed_device.event.dispatch import (
    apply_mapped_action_or_passthrough,
    is_recording_control_action,
    record_source_event,
)
from keymasq.keymasqd.runtime.grabbed_device.types import (
    EventProcessingDeps,
    GrabbedDeviceRuntime,
    InputEventLike,
)
from keymasq.keymasqd.runtime.rollover import (
    HeldMember,
    RolloverGroupState,
    RolloverOutput,
    RolloverRuntime,
    TrackedOutput,
    passthrough_output,
    rollover_output,
)


def source_button_id(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
) -> str | None:
    """Resolve a key event to its hardware button ID, mapped or not."""
    normalized_value = normalize_evdev_binding_value(int(event.type), int(event.value))
    button_id = device_runtime.event_binding_to_button.get(
        (int(event.type), int(event.code), normalized_value)
    ) or device_runtime.event_code_to_button.get((int(event.type), int(event.code)))
    if button_id:
        return button_id
    return device_runtime.evdev_to_button.get(event_name.lower())


async def intercept_rollover_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    deps: EventProcessingDeps,
) -> str | None:
    """Route a key event through its rollover group.

    Returns a diagnostics label when the group handled the event, or None when
    the event should continue through normal dispatch.
    """
    found = _member_group(device_runtime, event, event_name)
    group_state = found[1] if found is not None else None
    # Queued events can run after later presses of other keys, so each press
    # keeps the order it physically arrived in.
    press_seq = device_runtime.state.held_source_press_order.get(event_name, 0)
    # Queued events of this key go first, quarantine included: a queued press
    # may still need the release that arrives now.
    if _must_queue(device_runtime, event_name, group_state):
        # Record the physical event now: recording may stop before it runs.
        _record_member_event(device_runtime, event, event_name, deps=deps)
        _queue(
            device_runtime,
            group_state,
            event_name,
            _run_queued_member_event(
                device_runtime,
                event,
                event_name,
                epoch=device_runtime.state.rollover_epoch,
                press_seq=press_seq,
                deps=deps,
            ),
            deps=deps,
        )
        return "rollover_queued"
    quarantine_label = _pass_quarantine(device_runtime, event, event_name, deps=deps)
    if quarantine_label is not None:
        return quarantine_label
    if found is None:
        return None
    return await _run_member_event(
        device_runtime,
        event,
        event_name,
        trigger_runtime=device_runtime,
        epoch=None,
        press_seq=press_seq,
        deps=deps,
    )


def _pass_quarantine(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    deps: EventProcessingDeps,
    record: bool = True,
) -> str | None:
    """Swallow repeats and the release of a key whose output a group released."""
    value = int(event.value)
    quarantined = device_runtime.state.rollover_quarantined
    if event_name not in quarantined:
        return None
    if value == 0:
        quarantined.discard(event_name)
        if record:
            _record_member_event(device_runtime, event, event_name, deps=deps)
        return "rollover_release_suppressed"
    if value != 1:
        return "rollover_repeat_suppressed"
    # A fresh press drives its mapping again.
    quarantined.discard(event_name)
    return None


def _queued_event_label(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    epoch: int,
    deps: EventProcessingDeps,
) -> str | None:
    """Recheck a queued event once it runs. A label means it is done.

    Its device may have gone away while it waited, releasing everything it
    held, and its group may have been removed, quarantining the key.
    """
    if device_runtime.state.rollover_epoch != epoch:
        return "rollover_queued_dropped"
    return _pass_quarantine(device_runtime, event, event_name, deps=deps, record=False)


async def release_consumed_rollover_member(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    deps: EventProcessingDeps,
) -> None:
    """Update a group for a member release that an earlier stage consumed.

    Combo handling can take a member's release, for example after a combo
    recalled the key and released its output itself. The key is no longer
    held, so it leaves its group, and a member that is still held can take
    over. An output the group pressed after the combo recalled the key, for
    example from a queued press, is still released.
    """
    if int(event.value) != 0:
        return
    found = _member_group(device_runtime, event, event_name)
    group_state = found[1] if found is not None else None
    if _must_queue(device_runtime, event_name, group_state):
        _queue(
            device_runtime,
            group_state,
            event_name,
            _run_consumed_release(
                device_runtime,
                event,
                event_name,
                trigger_runtime=None,
                epoch=device_runtime.state.rollover_epoch,
                deps=deps,
            ),
            deps=deps,
        )
        return
    device_runtime.state.rollover_quarantined.discard(event_name)
    if found is None:
        return
    await _run_consumed_release(
        device_runtime,
        event,
        event_name,
        trigger_runtime=device_runtime,
        epoch=None,
        deps=deps,
    )


async def _run_member_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    trigger_runtime: GrabbedDeviceRuntime | None,
    epoch: int | None,
    press_seq: int,
    deps: EventProcessingDeps,
) -> str | None:
    """Apply a member event under its group's lock.

    ``epoch`` is set for a queued event, which is checked again once locked,
    and whose physical event was recorded when it arrived.
    """
    async with _LockedGroup(device_runtime, event, event_name) as found:
        if epoch is not None:
            label = _queued_event_label(device_runtime, event, event_name, epoch=epoch, deps=deps)
            if label is not None:
                return label
        if found is None:
            return None
        group_state, member = found
        return await _apply_member_event(
            group_state,
            member,
            device_runtime,
            event,
            event_name,
            trigger_runtime=trigger_runtime,
            record=epoch is None,
            press_seq=press_seq,
            deps=deps,
        )


async def _run_queued_member_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    epoch: int,
    press_seq: int,
    deps: EventProcessingDeps,
) -> None:
    # The event's SYN_REPORT already went by, so every dispatch flushes.
    label = await _run_member_event(
        device_runtime,
        event,
        event_name,
        trigger_runtime=None,
        epoch=epoch,
        press_seq=press_seq,
        deps=deps,
    )
    if label is None:
        # Normal dispatch, as the pipeline would have done inline.
        await apply_mapped_action_or_passthrough(
            device_runtime,
            event,
            event_name,
            device_runtime.mapping_getter(),
            recording_manager=device_runtime.recording_manager,
            combo_consumed=False,
            combo_passthrough_requested=False,
            deps=deps,
            record_input=False,
        )
        _flush_frame(device_runtime)


async def _run_consumed_release(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    trigger_runtime: GrabbedDeviceRuntime | None,
    epoch: int | None,
    deps: EventProcessingDeps,
) -> None:
    async with _LockedGroup(device_runtime, event, event_name) as found:
        if epoch is not None:
            if device_runtime.state.rollover_epoch != epoch:
                return
            device_runtime.state.rollover_quarantined.discard(event_name)
        if found is None:
            if epoch is not None and event_name in device_runtime.state.held_source_actions:
                # A queued press pressed it before its group went away, and
                # nothing else will release it.
                await apply_mapped_action_or_passthrough(
                    device_runtime,
                    event,
                    event_name,
                    device_runtime.mapping_getter(),
                    recording_manager=device_runtime.recording_manager,
                    combo_consumed=False,
                    combo_passthrough_requested=False,
                    deps=deps,
                    record_input=False,
                )
                _flush_frame(device_runtime)
            return
        group_state, member = found
        held = group_state.member(member)
        if held is None:
            return
        group_state.held.remove(held)
        group_state.lost.discard(member)
        # A recall already cleared ``pressed``. A member still pressed got its
        # output from the group after the combo ran, so the settle releases it.
        await _settle(group_state, trigger_runtime, int(event.type), deps=deps)


async def _apply_member_event(
    group_state: RolloverGroupState,
    member: RolloverMember,
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    trigger_runtime: GrabbedDeviceRuntime | None,
    record: bool,
    press_seq: int,
    deps: EventProcessingDeps,
) -> str | None:
    value = int(event.value)
    held = group_state.member(member)
    if value not in (0, 1):
        # Autorepeat belongs to whichever member drives the output.
        if held is None or (held is group_state.active and held.pressed):
            return None
        return "rollover_repeat_suppressed"

    if value == 1:
        if held is not None:
            if not held.recalled:
                return "rollover_press_suppressed"
            # The recalled key's release went missing. Its new press lets
            # it compete again, as the newest press.
            held.recalled = False
            held.fired = False
            held.withheld = _keyboard_withheld(device_runtime, event, event_name)
            held.press_seq = press_seq
            group_state.held.remove(held)
            bisect.insort(group_state.held, held, key=_press_order)
            group_state.lost.discard(member)
        else:
            bisect.insort(
                group_state.held,
                HeldMember(
                    member=member,
                    runtime=device_runtime,
                    event_code=int(event.code),
                    event_name=event_name,
                    press_seq=press_seq,
                    withheld=_keyboard_withheld(device_runtime, event, event_name),
                ),
                key=_press_order,
            )
    else:
        if held is None:
            return None
        group_state.held.remove(held)
        group_state.lost.discard(member)

    if record:
        _record_member_event(device_runtime, event, event_name, deps=deps)
    await _settle(group_state, trigger_runtime, int(event.type), deps=deps)
    return "rollover_member"


def _keyboard_withheld(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
) -> bool:
    action = find_action_for_code(
        device_runtime,
        int(event.type),
        int(event.code),
        1,
        event_name,
        device_runtime.mapping_getter(),
    )
    if action is None or action.action_type == ActionType.PASSTHROUGH:
        if passthrough_output(device_runtime, int(event.code))[0] != "passthrough":
            return False
        code = int(event.code)
    elif action.action_type == ActionType.KEYBOARD:
        code = resolve_output_code(action.target or "")
        if code is None:
            return False
    else:
        return False
    return outputs.keyboard_blocked(device_runtime, code)


def _record_member_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    deps: EventProcessingDeps,
) -> None:
    """Record a member's physical event, unless its key controls recording."""
    held_actions = device_runtime.state.held_source_actions
    if int(event.value) != 1 and event_name in held_actions:
        action = held_actions[event_name]
    else:
        action = find_action_for_code(
            device_runtime,
            int(event.type),
            int(event.code),
            int(event.value),
            event_name,
            device_runtime.mapping_getter(),
        )
    if not is_recording_control_action(action):
        record_source_event(device_runtime, event, deps=deps)


def _press_order(held: HeldMember) -> int:
    return held.press_seq


def _member_group(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
) -> tuple[RolloverRuntime, RolloverGroupState, RolloverMember] | None:
    getter = device_runtime.rollover_getter
    rollover = getter() if getter is not None else None
    if rollover is None:
        return None
    button_id = source_button_id(device_runtime, event, event_name)
    if button_id is None:
        return None
    member = RolloverMember(hardware_id=device_runtime.hardware_id, button=button_id)
    group_state = rollover.group_for(member)
    if group_state is None:
        return None
    return rollover, group_state, member


class _LockedGroup:
    """Hold the lock of the event's current group, found again once locked.

    A replacement can install a different group while the event waits, so
    the lookup repeats until the locked group is still the member's group.
    """

    def __init__(
        self,
        device_runtime: GrabbedDeviceRuntime,
        event: InputEventLike,
        event_name: str,
    ) -> None:
        self._device_runtime = device_runtime
        self._event = event
        self._event_name = event_name
        self._state: RolloverGroupState | None = None

    async def __aenter__(self) -> tuple[RolloverGroupState, RolloverMember] | None:
        while True:
            found = _member_group(self._device_runtime, self._event, self._event_name)
            if found is None:
                return None
            _rollover, state, member = found
            state.pending += 1
            try:
                await state.lock.acquire()
            except BaseException:
                state.pending -= 1
                raise
            current = _member_group(self._device_runtime, self._event, self._event_name)
            if current is not None and current[1] is state:
                self._state = state
                return state, member
            state.lock.release()
            state.pending -= 1

    async def __aexit__(self, *_exc: object) -> None:
        state = self._state
        if state is not None:
            state.lock.release()
            state.pending -= 1


def _group_busy(group_state: RolloverGroupState) -> bool:
    if group_state.pending > 0 or group_state.lock.locked():
        return True
    # Events still queued on a group this one replaced run first.
    group_state.predecessors = [
        previous for previous in group_state.predecessors if _group_busy(previous)
    ]
    return bool(group_state.predecessors)


def _must_queue(
    device_runtime: GrabbedDeviceRuntime,
    event_name: str,
    group_state: RolloverGroupState | None,
) -> bool:
    """Whether an event waits behind earlier rollover work.

    It does while its group is busy, and while an earlier event of the same
    key is still queued, even if the key's group changed or went away since.
    """
    tail = device_runtime.state.rollover_key_tails.get(event_name)
    if tail is not None and not tail.done():
        return True
    return group_state is not None and _group_busy(group_state)


def _queue(
    device_runtime: GrabbedDeviceRuntime,
    group_state: RolloverGroupState | None,
    event_name: str,
    run: Coroutine[object, object, object],
    *,
    deps: EventProcessingDeps,
) -> None:
    """Run ``run`` after earlier rollover work, off the device's event loop.

    A member action on another device can take a while, for example a natural
    mouse movement. Waiting inline would stall every later event of this
    device, including keys outside the group. Events of one key run in the
    order they arrived. Cleanup of the device cancels them.
    """
    state = device_runtime.state
    previous = state.rollover_key_tails.get(event_name)
    # Count the event as pending right away, so later events of the group
    # queue behind it even before its task starts.
    if group_state is not None:
        group_state.pending += 1
    label = f"rollover {group_state.group.name}" if group_state is not None else "rollover"
    task = deps.action_deps.fire_and_observe_fn(_after(previous, run), label)
    state.rollover_key_tails[event_name] = task
    state.rollover_tasks.add(task)

    def finished(done: asyncio.Task[object]) -> None:
        # A task cancelled before it started never runs its body, so the
        # bookkeeping lives here.
        if group_state is not None:
            group_state.pending -= 1
        run.close()
        state.rollover_tasks.discard(done)
        if state.rollover_key_tails.get(event_name) is done:
            del state.rollover_key_tails[event_name]

    task.add_done_callback(finished)


async def _after(
    previous: asyncio.Task[object] | None,
    run: Coroutine[object, object, object],
) -> None:
    if previous is not None and not previous.done():
        # Its outcome does not matter, only that it ran first.
        await asyncio.wait({previous})
    await run


def rollover_member_for_binding(
    device_runtime: GrabbedDeviceRuntime,
    evdev_name: str,
) -> tuple[RolloverRuntime, RolloverGroupState, HeldMember] | None:
    """Find the held rollover member behind a combo binding name."""
    getter = device_runtime.rollover_getter
    rollover = getter() if getter is not None else None
    if rollover is None:
        return None
    button_id = device_runtime.evdev_to_button.get(str(evdev_name or "").lower())
    if button_id is None:
        return None
    member = RolloverMember(hardware_id=device_runtime.hardware_id, button=button_id)
    group_state = rollover.group_for(member)
    held = group_state.member(member) if group_state is not None else None
    if group_state is None or held is None:
        return None
    return rollover, group_state, held


def recall_rollover_member(device_runtime: GrabbedDeviceRuntime, evdev_name: str) -> None:
    """A combo released a held member's output and keeps its key pending.

    The member stops competing until the combo restores it or the key is
    released, and a member that is still held can take over.
    """
    found = rollover_member_for_binding(device_runtime, evdev_name)
    if found is None:
        return
    rollover, group_state, held = found
    held.recalled = True
    held.pressed = False
    rollover.request_resettle(group_state)


def restore_rollover_member(device_runtime: GrabbedDeviceRuntime, evdev_name: str) -> bool:
    """Let a recalled member compete again. True when the group handles it.

    The group presses the member only if it wins, instead of the combo
    writing the output directly.
    """
    found = rollover_member_for_binding(device_runtime, evdev_name)
    if found is None:
        return False
    rollover, group_state, held = found
    if not held.recalled:
        return False
    held.recalled = False
    rollover.request_resettle(group_state)
    return True


def _flush_frame(runtime: GrabbedDeviceRuntime) -> None:
    outputs.flush_passthrough_frame(
        runtime,
        runtime.state.passthrough_frame_output,
        uinput_writer=identity_uinput_writer,
    )


async def resettle_rollover_group(
    rollover: RolloverRuntime,
    group_state: RolloverGroupState,
    *,
    deps: EventProcessingDeps,
) -> None:
    """Pick a new active member after members changed without a key event.

    This runs when a device goes away while one of its keys is held, when a
    combo recalls or restores a member, and when a changed group takes over
    held keys from the group it replaces.
    """
    group_state.pending += 1
    try:
        async with group_state.lock:
            if group_state not in rollover.states:
                return
            event_type = int(deps.evdev_mod.ecodes.EV_KEY)
            await _settle(group_state, None, event_type, deps=deps)
    finally:
        group_state.pending -= 1


async def _settle(
    group_state: RolloverGroupState,
    trigger_runtime: GrabbedDeviceRuntime | None,
    event_type: int,
    *,
    deps: EventProcessingDeps,
) -> None:
    previous = group_state.active
    chosen = group_state.next_active()
    group_state.mark_losers(chosen)
    chosen_output = _chosen_output(chosen, event_type) if chosen is not None else None

    # Every other pressed member gives up its output. Usually that is just the
    # previous active member, but a group that took over keys from several
    # old groups, or keys that were down outside any group, can have more.
    losers = [held for held in group_state.held if held.pressed and held is not chosen]
    if previous is not None and previous.pressed and previous is not chosen:
        if previous in losers:
            losers.remove(previous)
        losers.insert(0, previous)
    # Losers that write the chosen member's plain output hand it over: the
    # chosen press overwrites it, so skipping the release avoids passing
    # through rest, for example on an axis.
    donors: list[HeldMember] = []
    for held in losers:
        if chosen_output is not None and held.output == chosen_output:
            held.runtime.state.held_source_actions.pop(held.event_name, None)
            donors.append(held)
        else:
            await _dispatch(held, event_type, 0, trigger_runtime, deps=deps)
            held.tracked.clear()
        held.pressed = False

    group_state.active = chosen
    if chosen is None:
        return
    if chosen.pressed:
        if donors:
            await _reassert(chosen, donors, event_type, trigger_runtime, deps=deps)
        return
    await _press(chosen, donors, event_type, trigger_runtime, deps=deps)


def _chosen_output(chosen: HeldMember, event_type: int) -> RolloverOutput | None:
    if chosen.pressed:
        return chosen.output
    return rollover_output(
        find_action_for_code(
            chosen.runtime,
            event_type,
            chosen.event_code,
            1,
            chosen.event_name,
            chosen.runtime.mapping_getter(),
        ),
        passthrough=passthrough_output(chosen.runtime, chosen.event_code),
    )


async def _press(
    chosen: HeldMember,
    donors: list[HeldMember],
    event_type: int,
    trigger_runtime: GrabbedDeviceRuntime | None,
    *,
    deps: EventProcessingDeps,
) -> None:
    """Press the chosen member, taking over its donors' output."""
    tracked_before = _tracked_outputs(chosen.runtime)
    # Marked first, so the press is released later even if it is cancelled.
    chosen.pressed = True
    action = find_action_for_code(
        chosen.runtime,
        event_type,
        chosen.event_code,
        1,
        chosen.event_name,
        chosen.runtime.mapping_getter(),
    )
    if not (chosen.fired and action is not None and action.action_type in ONE_SHOT_ACTION_TYPES):
        # A one-shot action, such as a profile toggle, already ran for this
        # physical press and holds nothing, so taking over again skips it.
        await _dispatch(chosen, event_type, 1, trigger_runtime, deps=deps)
    chosen.fired = True
    chosen.tracked |= _tracked_outputs(chosen.runtime) - tracked_before
    _take_over_outputs(chosen, donors)
    chosen.output = rollover_output(
        chosen.runtime.state.held_source_actions.get(chosen.event_name),
        passthrough=passthrough_output(chosen.runtime, chosen.event_code),
    )


async def _reassert(
    chosen: HeldMember,
    donors: list[HeldMember],
    event_type: int,
    trigger_runtime: GrabbedDeviceRuntime | None,
    *,
    deps: EventProcessingDeps,
) -> None:
    """Keep an already pressed winner's output after donors wrote to it.

    This happens when a group takes over several held keys at once. A donor
    may have written the shared axis last, so the winner writes its value
    again, with the action it pressed with. A shared key or button is simply
    still down, and pulse outputs must not fire again.
    """
    _take_over_outputs(chosen, donors)
    if chosen.output is None or chosen.output[0] != "axis":
        return
    action = chosen.runtime.state.held_source_actions.get(chosen.event_name)
    if action is None:
        return
    await actions.execute_action(
        chosen.runtime,
        action,
        SyntheticInputEvent(event_type, chosen.event_code, 1),
        chosen.event_name,
        deps=deps.action_deps,
    )
    if chosen.runtime is not trigger_runtime:
        _flush_frame(chosen.runtime)


def _take_over_outputs(chosen: HeldMember, donors: list[HeldMember]) -> None:
    for donor in donors:
        if donor.runtime is not chosen.runtime:
            # The chosen member's device now owns the output. Otherwise the
            # donor's device would still release it when it disconnects. A
            # key adopted from outside a group has no record of its own, so
            # fall back to what both devices track.
            moved = donor.tracked or (
                _tracked_outputs(chosen.runtime) & _tracked_outputs(donor.runtime)
            )
            _forget_tracked_outputs(donor.runtime, moved)
            chosen.tracked |= moved
        donor.tracked.clear()


async def _dispatch(
    member: HeldMember,
    event_type: int,
    value: int,
    trigger_runtime: GrabbedDeviceRuntime | None,
    *,
    deps: EventProcessingDeps,
) -> None:
    runtime = member.runtime
    await apply_mapped_action_or_passthrough(
        runtime,
        SyntheticInputEvent(event_type, member.event_code, value),
        member.event_name,
        runtime.mapping_getter(),
        recording_manager=runtime.recording_manager,
        combo_consumed=False,
        combo_passthrough_requested=False,
        deps=deps,
        record_input=False,
    )
    if runtime is not trigger_runtime:
        # Only the triggering interface's own SYN_REPORT closes its frame.
        _flush_frame(runtime)


def _tracked_outputs(runtime: GrabbedDeviceRuntime) -> set[TrackedOutput]:
    """Held keys and axes a runtime releases when it goes away."""
    state = runtime.state
    tracked = {
        ("key", bucket, code) for bucket, codes in state.held_output_keys.items() for code in codes
    }
    tracked.update(
        ("abs", bucket, code) for bucket, codes in state.held_output_abs.items() for code in codes
    )
    return tracked


def _forget_tracked_outputs(runtime: GrabbedDeviceRuntime, moved: set[TrackedOutput]) -> None:
    state = runtime.state
    for kind, bucket, code in moved:
        if kind == "key":
            state.held_output_keys.get(bucket, set()).discard(code)
        else:
            state.held_output_abs.get(bucket, set()).discard(code)
            # Releasing this device's outputs resets untracked trigger axes.
            # This one belongs to another device now.
            state.rollover_abs_handed_over.setdefault(bucket, set()).add(code)
