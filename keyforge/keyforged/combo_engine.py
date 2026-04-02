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
        self._candidates: dict[str, ActiveCandidate] = {}
        self._press_counter = 0

    def set_combos(self, combos: list[RuntimeCombo]) -> None:
        self._combos = list(combos)
        self._combo_order = {combo.id: index for index, combo in enumerate(self._combos)}
        self.reset()

    def reset(self) -> None:
        self._candidates.clear()
        self._press_counter = 0

    def prime_held_bindings(self, held_bindings: set[RuntimeComboBinding]) -> None:
        if self._candidates or not held_bindings:
            return
        self._candidates = self._build_fresh_candidates_for_held_bindings(held_bindings)

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
        if not self._candidates:
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
        if int(event.value) == 2:
            if expired and not self._candidates:
                decision.reset_candidates = True
            return decision

        had_candidates = bool(self._candidates)
        if any(candidate.releasing for candidate in self._candidates.values()):
            decision = self._handle_releasing_event(event, now_monotonic)
        elif self._candidates:
            decision = self._handle_candidate_event(event, now_monotonic)
        else:
            decision = self._handle_fresh_event(event, now_monotonic)

        if expired and had_candidates and not self._candidates:
            decision.reset_candidates = True
        elif had_candidates and not self._candidates and (
            decision.consume_current_event or decision.passthrough_current_event
        ):
            decision.reset_candidates = True
        return decision

    def _handle_fresh_event(
        self,
        event: ComboInputEvent,
        _now_monotonic: float,
    ) -> ComboDecision:
        if int(event.value) != 1:
            return ComboDecision()

        matching = [
            combo
            for combo in self._combos
            if self._step_matches_binding(combo.steps[0], event.binding)
        ]
        if not matching:
            return ComboDecision()

        final_complete: list[RuntimeCombo] = []
        nonfinal_complete: list[RuntimeCombo] = []
        incomplete: list[RuntimeCombo] = []
        for combo in matching:
            if len(combo.steps[0].bindings) == 1:
                if len(combo.steps) == 1:
                    final_complete.append(combo)
                else:
                    nonfinal_complete.append(combo)
            else:
                incomplete.append(combo)

        if final_complete:
            winner = self._choose_shortest_complete(final_complete)
            candidate = self._new_candidate(winner)
            self._complete_current_step(candidate, event.binding, is_final=True)
            self._candidates = {winner.id: candidate}
            return ComboDecision(
                consume_current_event=True,
                recall_events=[],
                action_transition=ComboActionTransition(
                    combo_id=winner.id,
                    action=winner.action,
                    kind="press",
                    trigger_binding=event.binding,
                ),
            )

        if nonfinal_complete:
            kept: dict[str, ActiveCandidate] = {}
            for combo in nonfinal_complete:
                candidate = self._new_candidate(combo)
                self._complete_current_step(candidate, event.binding, is_final=False)
                kept[combo.id] = candidate
            self._candidates = kept
            return ComboDecision(consume_current_event=True)

        kept = {}
        for combo in incomplete:
            candidate = self._new_candidate(combo)
            self._track_press(candidate, event.binding, passed_through=True)
            kept[combo.id] = candidate
        self._candidates = kept
        return ComboDecision(passthrough_current_event=True)

    def _handle_candidate_event(
        self,
        event: ComboInputEvent,
        now_monotonic: float,
    ) -> ComboDecision:
        if int(event.value) == 0:
            return self._handle_release_before_completion(event)
        if int(event.value) != 1:
            return ComboDecision()

        matching_ids: list[str] = []
        repeated_ids: list[str] = []
        for combo_id, candidate in self._candidates.items():
            if event.binding in candidate.pressed_bindings:
                repeated_ids.append(combo_id)
                continue
            if self._step_matches_binding(candidate.current_step(), event.binding):
                matching_ids.append(combo_id)

        if repeated_ids and not matching_ids:
            return ComboDecision(consume_current_event=True)

        if not matching_ids:
            self._candidates.clear()
            return ComboDecision(passthrough_current_event=True, reset_candidates=True)

        complete_ids: list[str] = []
        final_complete_ids: list[str] = []
        for combo_id in matching_ids:
            candidate = self._candidates[combo_id]
            if len(candidate.pressed_bindings) + 1 == len(candidate.current_step().bindings):
                complete_ids.append(combo_id)
                if candidate.step_index >= len(candidate.combo.steps) - 1:
                    final_complete_ids.append(combo_id)

        if final_complete_ids:
            final_combos = [self._candidates[combo_id].combo for combo_id in final_complete_ids]
            winner = self._choose_shortest_complete(final_combos)
            candidate = self._candidates[winner.id]
            self._complete_current_step(candidate, event.binding, is_final=True)
            self._candidates = {winner.id: candidate}
            return ComboDecision(
                consume_current_event=True,
                recall_events=self._recall_events(candidate),
                action_transition=ComboActionTransition(
                    combo_id=winner.id,
                    action=winner.action,
                    kind="press",
                    trigger_binding=event.binding,
                ),
            )

        if complete_ids:
            kept: dict[str, ActiveCandidate] = {}
            recall_events: list[ComboSyntheticEvent] = []
            for index, combo_id in enumerate(complete_ids):
                candidate = self._candidates[combo_id]
                self._complete_current_step(candidate, event.binding, is_final=False)
                if index == 0:
                    recall_events = self._recall_events(candidate)
                kept[combo_id] = candidate
            self._candidates = kept
            return ComboDecision(
                consume_current_event=True,
                recall_events=recall_events,
            )

        kept = {}
        for combo_id in matching_ids:
            candidate = self._candidates[combo_id]
            self._track_press(candidate, event.binding, passed_through=True)
            kept[combo_id] = candidate
        self._candidates = kept
        return ComboDecision(passthrough_current_event=True)

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

    def _handle_releasing_event(
        self,
        event: ComboInputEvent,
        now_monotonic: float,
    ) -> ComboDecision:
        decision = ComboDecision()
        kept: dict[str, ActiveCandidate] = {}
        matched_step_binding = False
        rearm_bindings: set[RuntimeComboBinding] = set()
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
                        rearm_bindings.update(candidate.pressed_bindings)
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
        if rearm_bindings:
            rebuilt = self._build_fresh_candidates_for_held_bindings(rearm_bindings)
            rebuilt.update(kept)
            self._candidates = rebuilt
        else:
            self._candidates = kept
        if matched_step_binding:
            decision.consume_current_event = True
            if not self._candidates:
                decision.reset_candidates = True
            return decision
        if int(event.value) == 1:
            sibling_decision = self._try_complete_single_step_from_held_releasing(
                event,
                kept,
            )
            if sibling_decision is not None:
                return sibling_decision
        return ComboDecision(passthrough_current_event=True)

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

    def _build_fresh_candidates_for_held_bindings(
        self,
        held_bindings: set[RuntimeComboBinding],
    ) -> dict[str, ActiveCandidate]:
        if not held_bindings:
            return {}
        candidates: dict[str, ActiveCandidate] = {}
        for combo in self._combos:
            first_step = combo.steps[0]
            matching_held = {
                binding
                for binding in held_bindings
                if self._step_matches_binding(first_step, binding)
            }
            if not matching_held or len(matching_held) >= len(first_step.bindings):
                continue
            candidate = self._new_candidate(combo)
            for binding in matching_held:
                self._track_press(candidate, binding, passed_through=False)
            candidates[combo.id] = candidate
        return candidates

    def _try_complete_single_step_from_held_releasing(
        self,
        event: ComboInputEvent,
        kept: dict[str, ActiveCandidate],
    ) -> ComboDecision | None:
        held_bindings: set[RuntimeComboBinding] = set()
        for candidate in kept.values():
            if (
                candidate.releasing
                and candidate.step_index == 0
                and len(candidate.combo.steps) == 1
            ):
                held_bindings.update(candidate.pressed_bindings)

        if not held_bindings:
            return None

        eligible: list[tuple[RuntimeCombo, set[RuntimeComboBinding]]] = []
        for combo in self._combos:
            if len(combo.steps) != 1:
                continue
            first_step = combo.steps[0]
            if not self._step_matches_binding(first_step, event.binding):
                continue
            matching_held = {
                binding
                for binding in held_bindings
                if self._step_matches_binding(first_step, binding)
            }
            if event.binding in matching_held:
                continue
            if len(matching_held) + 1 != len(first_step.bindings):
                continue
            eligible.append((combo, matching_held))

        if not eligible:
            return None

        winner = self._choose_shortest_complete([combo for combo, _bindings in eligible])
        winner_bindings = next(
            bindings for combo, bindings in eligible if combo.id == winner.id
        )
        candidate = self._new_candidate(winner)
        for binding in winner_bindings:
            self._track_press(candidate, binding, passed_through=False)
        self._complete_current_step(candidate, event.binding, is_final=True)
        kept[winner.id] = candidate
        self._candidates = kept
        return ComboDecision(
            consume_current_event=True,
            action_transition=ComboActionTransition(
                combo_id=winner.id,
                action=winner.action,
                kind="press",
                trigger_binding=event.binding,
            ),
        )

    def _recall_events(self, candidate: ActiveCandidate) -> list[ComboSyntheticEvent]:
        return [
            ComboSyntheticEvent(binding=tracked.binding, value=0)
            for tracked in sorted(
                (
                    tracked
                    for tracked in candidate.tracked_presses
                    if tracked.passed_through
                    and tracked.binding != candidate.final_completing_binding
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

    def _choose_shortest_complete(self, combos: list[RuntimeCombo]) -> RuntimeCombo:
        return min(
            combos,
            key=lambda combo: (
                len(combo.steps),
                sum(len(step.bindings) for step in combo.steps),
                self._combo_order.get(combo.id, 0),
            ),
        )

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
