from __future__ import annotations

from dataclasses import dataclass, field

from keyforge.common.combos import GENERIC_MODIFIER_MAP, normalize_combo_evdev
from keyforge.common.models import MappingAction

DEFAULT_COMBO_STEP_TIMEOUT_MS = 600
GENERIC_MODIFIERS = frozenset(GENERIC_MODIFIER_MAP.values())


@dataclass(frozen=True)
class RuntimeComboBinding:
    hardware_id: str
    evdev: str
    source: str = ""


@dataclass(frozen=True)
class RuntimeComboStep:
    bindings: tuple[RuntimeComboBinding, ...]
    timeout_ms: int | None = None


@dataclass
class RuntimeCombo:
    id: str
    name: str
    steps: list[RuntimeComboStep] = field(default_factory=list)
    action: MappingAction | None = None
    profile_name: str = ""
    recall_trigger_keys: bool = False
    restore_trigger_keys: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ComboInputEvent:
    binding: RuntimeComboBinding
    value: int


@dataclass(frozen=True)
class ComboSyntheticEvent:
    binding: RuntimeComboBinding
    value: int = 0


@dataclass(frozen=True)
class ComboActionTransition:
    combo_id: str
    action: MappingAction | None
    kind: str
    trigger_binding: RuntimeComboBinding
    trigger_bindings: tuple[RuntimeComboBinding, ...] = ()


@dataclass
class ComboDecision:
    consume_current_event: bool = False
    passthrough_current_event: bool = False
    recall_events: list[ComboSyntheticEvent] = field(default_factory=list)
    action_transition: ComboActionTransition | None = None
    extra_action_transitions: list[ComboActionTransition] = field(default_factory=list)
    reset_candidates: bool = False


@dataclass
class TrackedPress:
    binding: RuntimeComboBinding
    press_order: int
    passed_through: bool


@dataclass
class ActiveCandidate:
    combo: RuntimeCombo
    step_index: int = 0
    tracked_presses: list[TrackedPress] = field(default_factory=list)
    pressed_bindings: set[RuntimeComboBinding] = field(default_factory=set)
    releasing: bool = False
    next_step_deadline_monotonic: float | None = None
    final_completing_binding: RuntimeComboBinding | None = None
    action_active: bool = False

    def current_step(self) -> RuntimeComboStep:
        return self.combo.steps[self.step_index]


class ComboEngine:
    def __init__(self) -> None:
        self._combos: list[RuntimeCombo] = []
        self._combo_order: dict[str, int] = {}
        self._first_step_binding_index: dict[tuple[str, str, str], list[str]] = {}
        self._candidates: dict[str, ActiveCandidate] = {}
        self._press_counter = 0
        self._held_bindings: set[RuntimeComboBinding] = set()
        self._held_press_order: dict[RuntimeComboBinding, int] = {}

    def set_combos(self, combos: list[RuntimeCombo]) -> None:
        self._combos = list(combos)
        self._combo_order = {combo.id: index for index, combo in enumerate(self._combos)}
        self._first_step_binding_index.clear()
        for combo in self._combos:
            if not combo.steps:
                continue
            for binding in combo.steps[0].bindings:
                key = (
                    binding.hardware_id,
                    normalize_combo_evdev(binding.evdev),
                    binding.source,
                )
                self._first_step_binding_index.setdefault(key, []).append(combo.id)
        self.reset()

    def reset(self) -> None:
        self._candidates.clear()
        self._press_counter = 0
        self._held_bindings.clear()
        self._held_press_order.clear()

    def prime_held_bindings(self, held_bindings: set[RuntimeComboBinding]) -> None:
        for binding in sorted(
            held_bindings,
            key=lambda binding: (binding.hardware_id, binding.source, binding.evdev),
        ):
            self._record_held_press(binding)

    def drop_candidates_for_binding_scope(
        self,
        hardware_id: str,
        source: str | None = None,
    ) -> set[str]:
        normalized_hardware_id = str(hardware_id or "")
        if not normalized_hardware_id:
            return set()

        normalized_source = None if source is None else str(source or "")
        active_combo_ids: set[str] = set()
        kept: dict[str, ActiveCandidate] = {}
        for combo_id, candidate in self._candidates.items():
            if not self._candidate_matches_scope(
                candidate,
                normalized_hardware_id,
                normalized_source,
            ):
                kept[combo_id] = candidate
                continue
            if candidate.action_active:
                active_combo_ids.add(combo_id)

        self._candidates = kept
        self._held_bindings = {
            binding
            for binding in self._held_bindings
            if not self._binding_in_scope(binding, normalized_hardware_id, normalized_source)
        }
        self._held_press_order = {
            binding: order
            for binding, order in self._held_press_order.items()
            if not self._binding_in_scope(binding, normalized_hardware_id, normalized_source)
        }
        if not self._candidates and not self._held_bindings:
            self._press_counter = 0
        return active_combo_ids

    def next_deadline(self) -> float | None:
        deadlines = [
            candidate.next_step_deadline_monotonic
            for candidate in self._candidates.values()
            if candidate.next_step_deadline_monotonic is not None
        ]
        if not deadlines:
            return None
        return min(deadlines)

    def expire_timeouts(self, now_monotonic: float) -> bool:
        expired_ids = [
            combo_id
            for combo_id, candidate in self._candidates.items()
            if candidate.next_step_deadline_monotonic is not None
            and candidate.next_step_deadline_monotonic <= now_monotonic
        ]
        if not expired_ids:
            return False
        for combo_id in expired_ids:
            self._candidates.pop(combo_id, None)
        return True

    def handle_event(self, event: ComboInputEvent, now_monotonic: float) -> ComboDecision:
        decision = ComboDecision()
        expired = self.expire_timeouts(now_monotonic)
        value = int(event.value)
        if value == 2:
            if expired and not self._candidates:
                decision.reset_candidates = True
            return decision

        had_candidates = bool(self._candidates)
        if value == 1:
            self._record_held_press(event.binding)
            decision = self._handle_press_event(event, now_monotonic)
        else:
            decision = self._handle_release_event(event, now_monotonic)
            self._held_bindings.discard(event.binding)
            self._held_press_order.pop(event.binding, None)

        if expired and had_candidates and not self._candidates:
            decision.reset_candidates = True
        elif had_candidates and not self._candidates and (
            decision.consume_current_event or decision.passthrough_current_event
        ):
            decision.reset_candidates = True
        if not self._candidates and not self._held_bindings:
            self._press_counter = 0
        return decision

    def _handle_press_event(
        self,
        event: ComboInputEvent,
        now_monotonic: float,
    ) -> ComboDecision:
        waiting = self._handle_waiting_candidate_press(event, now_monotonic)
        first_step = self._activate_satisfied_first_steps(event)
        decision = self._merge_decisions(waiting, first_step)
        if not decision.consume_current_event and not decision.passthrough_current_event:
            if self._binding_matches_any_first_step(event.binding):
                decision.passthrough_current_event = True
        return decision

    def _handle_waiting_candidate_press(
        self,
        event: ComboInputEvent,
        now_monotonic: float,
    ) -> ComboDecision:
        waiting_candidates = {
            combo_id: candidate
            for combo_id, candidate in self._candidates.items()
            if not candidate.releasing
        }
        if not waiting_candidates:
            return ComboDecision()

        matching_ids: list[str] = []
        repeated_ids: list[str] = []
        scoped_ids: list[str] = []
        for combo_id, candidate in waiting_candidates.items():
            if event.binding in candidate.pressed_bindings:
                repeated_ids.append(combo_id)
                scoped_ids.append(combo_id)
                continue
            if self._step_matches_binding(candidate.current_step(), event.binding):
                matching_ids.append(combo_id)
                scoped_ids.append(combo_id)
                continue
            if self._step_matches_scope(candidate.current_step(), event.binding):
                scoped_ids.append(combo_id)

        if repeated_ids and not matching_ids:
            return ComboDecision(consume_current_event=True)

        if not matching_ids:
            if not scoped_ids:
                return ComboDecision()
            for combo_id in scoped_ids:
                self._candidates.pop(combo_id, None)
            return ComboDecision(
                passthrough_current_event=True,
                reset_candidates=not self._candidates,
            )

        decision = ComboDecision()
        transitions: list[ComboActionTransition] = []
        recall_events: list[ComboSyntheticEvent] = []
        partial_match = False
        for combo_id in sorted(
            matching_ids,
            key=lambda combo_id: self._combo_order.get(combo_id, 0),
        ):
            candidate = self._candidates[combo_id]
            if len(candidate.pressed_bindings) + 1 == len(candidate.current_step().bindings):
                self._complete_current_step(
                    candidate,
                    event.binding,
                    is_final=candidate.step_index >= len(candidate.combo.steps) - 1,
                )
                recall_events.extend(self._recall_events(candidate))
                if candidate.action_active:
                    transitions.append(
                        ComboActionTransition(
                            combo_id=candidate.combo.id,
                            action=candidate.combo.action,
                            kind="press",
                            trigger_binding=event.binding,
                            trigger_bindings=self._trigger_bindings(candidate),
                        )
                    )
            else:
                partial_match = True
                self._track_press(candidate, event.binding, passed_through=True)

        decision.consume_current_event = bool(transitions) or any(
            self._candidates[combo_id].releasing
            for combo_id in matching_ids
            if combo_id in self._candidates
        )
        if partial_match and not decision.consume_current_event:
            decision.passthrough_current_event = True
        if transitions:
            decision.action_transition = transitions[0]
            decision.extra_action_transitions = transitions[1:]
        decision.recall_events = self._dedupe_recall_events(recall_events)
        return decision

    def _handle_release_before_completion(self, event: ComboInputEvent) -> ComboDecision:
        kept: dict[str, ActiveCandidate] = {}
        dropped = False
        released_was_passthrough = False
        for combo_id, candidate in self._candidates.items():
            if event.binding in candidate.pressed_bindings:
                dropped = True
                released_was_passthrough = (
                    released_was_passthrough
                    or self._binding_was_passed_through(candidate, event.binding)
                )
                continue
            kept[combo_id] = candidate
        if not dropped:
            return ComboDecision()
        self._candidates = kept
        if released_was_passthrough:
            return ComboDecision(
                passthrough_current_event=True,
                reset_candidates=not kept,
            )
        return ComboDecision(
            consume_current_event=True,
            reset_candidates=not kept,
        )

    def _handle_release_event(
        self,
        event: ComboInputEvent,
        now_monotonic: float,
    ) -> ComboDecision:
        if any(candidate.releasing for candidate in self._candidates.values()):
            return self._handle_releasing_event(event, now_monotonic)
        return self._handle_release_before_completion(event)

    def _handle_releasing_event(
        self,
        event: ComboInputEvent,
        now_monotonic: float,
    ) -> ComboDecision:
        decision = ComboDecision()
        kept: dict[str, ActiveCandidate] = {}
        matched_step_binding = False
        transitions: list[ComboActionTransition] = []
        for combo_id, candidate in self._candidates.items():
            if not candidate.releasing:
                kept[combo_id] = candidate
                continue
            step = candidate.current_step()
            if not self._step_matches_binding(step, event.binding):
                kept[combo_id] = candidate
                continue

            matched_step_binding = True
            if int(event.value) == 0:
                if candidate.action_active:
                    transitions.append(
                        ComboActionTransition(
                            combo_id=candidate.combo.id,
                            action=candidate.combo.action,
                            kind="release",
                            trigger_binding=event.binding,
                        )
                    )
                    candidate.action_active = False
                candidate.pressed_bindings.discard(event.binding)
                if candidate.pressed_bindings:
                    if candidate.step_index == 0 and len(candidate.combo.steps) == 1:
                        continue
                    kept[combo_id] = candidate
                    continue
                if candidate.step_index >= len(candidate.combo.steps) - 1:
                    continue
                candidate.step_index += 1
                candidate.tracked_presses.clear()
                candidate.releasing = False
                candidate.final_completing_binding = None
                candidate.next_step_deadline_monotonic = (
                    now_monotonic + self._effective_timeout_ms(candidate.current_step()) / 1000.0
                )
                kept[combo_id] = candidate
                continue

            kept[combo_id] = candidate

        if transitions:
            decision.action_transition = transitions[0]
            decision.extra_action_transitions = transitions[1:]
        self._candidates = kept
        if matched_step_binding:
            decision.consume_current_event = True
            if not self._candidates:
                decision.reset_candidates = True
            return decision
        return ComboDecision()

    def _new_candidate(self, combo: RuntimeCombo) -> ActiveCandidate:
        return ActiveCandidate(combo=combo)

    def _track_press(
        self,
        candidate: ActiveCandidate,
        binding: RuntimeComboBinding,
        passed_through: bool,
    ) -> None:
        if binding in candidate.pressed_bindings:
            return
        self._press_counter += 1
        candidate.pressed_bindings.add(binding)
        candidate.next_step_deadline_monotonic = None
        candidate.tracked_presses.append(
            TrackedPress(
                binding=binding,
                press_order=self._press_counter,
                passed_through=passed_through,
            )
        )

    def _complete_current_step(
        self,
        candidate: ActiveCandidate,
        completing_binding: RuntimeComboBinding,
        is_final: bool,
    ) -> None:
        if completing_binding not in candidate.pressed_bindings:
            candidate.pressed_bindings.add(completing_binding)
        candidate.releasing = True
        candidate.next_step_deadline_monotonic = None
        if is_final:
            candidate.final_completing_binding = completing_binding
            candidate.action_active = True
        else:
            candidate.final_completing_binding = None
            candidate.action_active = False

    def _activate_satisfied_first_steps(self, event: ComboInputEvent) -> ComboDecision:
        decision = ComboDecision()
        transitions: list[ComboActionTransition] = []
        recall_events: list[ComboSyntheticEvent] = []

        for combo in self._candidate_first_step_combos(event.binding):
            if combo.id in self._candidates:
                continue
            held_bindings = self._held_bindings_for_step(combo.steps[0])
            if held_bindings is None:
                continue
            candidate = self._new_candidate(combo)
            ordered_bindings = sorted(
                held_bindings,
                key=lambda binding: self._held_press_order.get(binding, 0),
            )
            for binding in ordered_bindings:
                self._track_press(candidate, binding, passed_through=binding != event.binding)
            self._complete_current_step(candidate, event.binding, is_final=len(combo.steps) == 1)
            self._candidates[combo.id] = candidate
            recall_events.extend(self._recall_events(candidate))
            if candidate.action_active:
                transitions.append(
                    ComboActionTransition(
                        combo_id=combo.id,
                        action=combo.action,
                        kind="press",
                        trigger_binding=event.binding,
                        trigger_bindings=self._trigger_bindings(candidate),
                    )
                )

        if not transitions and not any(
            combo_id in self._candidates
            for combo_id in (
                combo.id for combo in self._candidate_first_step_combos(event.binding)
            )
        ):
            return ComboDecision()

        decision.consume_current_event = True
        decision.recall_events = self._dedupe_recall_events(recall_events)
        if transitions:
            decision.action_transition = transitions[0]
            decision.extra_action_transitions = transitions[1:]
        return decision

    def _trigger_bindings(self, candidate: ActiveCandidate) -> tuple[RuntimeComboBinding, ...]:
        return tuple(
            tracked.binding
            for tracked in sorted(
                candidate.tracked_presses,
                key=lambda tracked: tracked.press_order,
            )
        )

    def _recall_events(self, candidate: ActiveCandidate) -> list[ComboSyntheticEvent]:
        return [
            ComboSyntheticEvent(binding=tracked.binding, value=0)
            for tracked in sorted(
                (
                    tracked
                    for tracked in candidate.tracked_presses
                    if tracked.binding != candidate.final_completing_binding
                    and not self._binding_is_modifier(tracked.binding)
                ),
                key=lambda tracked: tracked.press_order,
                reverse=True,
            )
        ]

    def _binding_is_modifier(self, binding: RuntimeComboBinding) -> bool:
        normalized = normalize_combo_evdev(binding.evdev)
        return normalized in GENERIC_MODIFIERS

    def _binding_was_passed_through(
        self,
        candidate: ActiveCandidate,
        binding: RuntimeComboBinding,
    ) -> bool:
        for tracked in candidate.tracked_presses:
            if tracked.binding == binding:
                return tracked.passed_through
        return False

    def _step_matches_binding(
        self,
        step: RuntimeComboStep,
        binding: RuntimeComboBinding,
    ) -> bool:
        for step_binding in step.bindings:
            if self._binding_matches(step_binding, binding):
                return True
        return False

    def _binding_matches(
        self,
        expected: RuntimeComboBinding,
        actual: RuntimeComboBinding,
    ) -> bool:
        return (
            expected.hardware_id == actual.hardware_id
            and normalize_combo_evdev(expected.evdev) == normalize_combo_evdev(actual.evdev)
            and (not expected.source or expected.source == actual.source)
        )

    def _step_matches_scope(
        self,
        step: RuntimeComboStep,
        binding: RuntimeComboBinding,
    ) -> bool:
        return any(
            step_binding.hardware_id == binding.hardware_id
            and (not step_binding.source or step_binding.source == binding.source)
            for step_binding in step.bindings
        )

    def _record_held_press(self, binding: RuntimeComboBinding) -> None:
        if binding in self._held_bindings:
            return
        self._press_counter += 1
        self._held_bindings.add(binding)
        self._held_press_order[binding] = self._press_counter

    def _candidate_first_step_combos(self, binding: RuntimeComboBinding) -> list[RuntimeCombo]:
        normalized = normalize_combo_evdev(binding.evdev)
        combo_ids: list[str] = []
        combo_ids.extend(
            self._first_step_binding_index.get(
                (binding.hardware_id, normalized, binding.source),
                [],
            )
        )
        combo_ids.extend(
            self._first_step_binding_index.get(
                (binding.hardware_id, normalized, ""),
                [],
            )
        )
        seen: set[str] = set()
        combos: list[RuntimeCombo] = []
        combo_map = {combo.id: combo for combo in self._combos}
        for combo_id in combo_ids:
            if combo_id in seen:
                continue
            seen.add(combo_id)
            combo = combo_map.get(combo_id)
            if combo is None:
                continue
            combos.append(combo)
        return combos

    def _binding_matches_any_first_step(self, binding: RuntimeComboBinding) -> bool:
        return bool(self._candidate_first_step_combos(binding))

    def _held_bindings_for_step(
        self,
        step: RuntimeComboStep,
    ) -> set[RuntimeComboBinding] | None:
        matched: set[RuntimeComboBinding] = set()
        for expected in step.bindings:
            actual = next(
                (
                    held
                    for held in self._held_bindings
                    if held not in matched and self._binding_matches(expected, held)
                ),
                None,
            )
            if actual is None:
                return None
            matched.add(actual)
        return matched

    def _dedupe_recall_events(
        self,
        events: list[ComboSyntheticEvent],
    ) -> list[ComboSyntheticEvent]:
        seen: set[RuntimeComboBinding] = set()
        deduped: list[ComboSyntheticEvent] = []
        for event in events:
            if event.binding in seen:
                continue
            seen.add(event.binding)
            deduped.append(event)
        return deduped

    def _merge_decisions(self, *decisions: ComboDecision) -> ComboDecision:
        merged = ComboDecision()
        transitions: list[ComboActionTransition] = []
        recall_events: list[ComboSyntheticEvent] = []
        for decision in decisions:
            merged.consume_current_event = (
                merged.consume_current_event or decision.consume_current_event
            )
            merged.passthrough_current_event = (
                merged.passthrough_current_event or decision.passthrough_current_event
            )
            merged.reset_candidates = merged.reset_candidates or decision.reset_candidates
            recall_events.extend(decision.recall_events)
            if decision.action_transition is not None:
                transitions.append(decision.action_transition)
            transitions.extend(decision.extra_action_transitions)
        merged.recall_events = self._dedupe_recall_events(recall_events)
        if transitions:
            merged.action_transition = transitions[0]
            merged.extra_action_transitions = transitions[1:]
        if merged.consume_current_event:
            merged.passthrough_current_event = False
        return merged

    def _effective_timeout_ms(self, step: RuntimeComboStep) -> int:
        if step.timeout_ms is None:
            return DEFAULT_COMBO_STEP_TIMEOUT_MS
        return max(1, int(step.timeout_ms))

    def _candidate_matches_scope(
        self,
        candidate: ActiveCandidate,
        hardware_id: str,
        source: str | None,
    ) -> bool:
        bindings: set[RuntimeComboBinding] = set(candidate.pressed_bindings)
        bindings.update(tracked.binding for tracked in candidate.tracked_presses)
        if candidate.final_completing_binding is not None:
            bindings.add(candidate.final_completing_binding)
        return any(
            self._binding_in_scope(binding, hardware_id, source)
            for binding in bindings
        )

    def _binding_in_scope(
        self,
        binding: RuntimeComboBinding,
        hardware_id: str,
        source: str | None,
    ) -> bool:
        if binding.hardware_id != hardware_id:
            return False
        if source is None:
            return True
        return binding.source == source
