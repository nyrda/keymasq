import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass

from keymasq.common.model.actions import ProfileDeactivationPolicy
from keymasq.common.types import JsonObject

type BroadcastDeactivateRequest = Callable[[JsonObject], None]

log = logging.getLogger("keymasqd.runtime.profile_activation_tracker")


@dataclass
class RuntimeProfileActivationTracker:
    profile_name: str
    activation_id: str
    trigger_id: str
    deactivation: ProfileDeactivationPolicy
    action_count: int = 0
    expired: bool = False
    timeout_task: asyncio.Task[None] | None = None


class ProfileActivationTracker:
    def __init__(
        self,
        *,
        broadcast_deactivate_request: BroadcastDeactivateRequest,
    ) -> None:
        self._broadcast_deactivate_request = broadcast_deactivate_request
        self._trackers: dict[str, RuntimeProfileActivationTracker] = {}
        self._activation_by_profile: dict[str, str] = {}
        self._active_trigger_ids: set[str] = set()

    def track(
        self,
        *,
        profile_name: str,
        activation_id: str,
        trigger_id: str,
        deactivation: ProfileDeactivationPolicy,
    ) -> None:
        normalized_profile_name = str(profile_name or "").strip()
        normalized_activation_id = str(activation_id or "").strip()
        normalized_trigger_id = str(trigger_id or "").strip()
        if (
            not normalized_profile_name
            or not normalized_activation_id
            or not deactivation.has_condition
        ):
            return

        previous_activation_id = self._activation_by_profile.get(normalized_profile_name)
        if previous_activation_id and previous_activation_id != normalized_activation_id:
            self.cancel(activation_id=previous_activation_id)
        previous_tracker = self._trackers.get(normalized_activation_id)
        if previous_tracker is not None:
            self._cancel_timeout(previous_tracker)

        tracker = RuntimeProfileActivationTracker(
            profile_name=normalized_profile_name,
            activation_id=normalized_activation_id,
            trigger_id=normalized_trigger_id,
            deactivation=deactivation,
        )
        self._trackers[normalized_activation_id] = tracker
        self._activation_by_profile[normalized_profile_name] = normalized_activation_id

        if deactivation.timeout_ms is not None:
            tracker.timeout_task = asyncio.create_task(
                self._timeout_after(tracker, int(deactivation.timeout_ms) / 1000.0)
            )

        if deactivation.on_trigger_end and normalized_trigger_id:
            if normalized_trigger_id not in self._active_trigger_ids:
                self._expire(tracker, "trigger_end")

    def cancel(
        self,
        *,
        profile_name: str | None = None,
        activation_id: str | None = None,
    ) -> None:
        normalized_activation_id = str(activation_id or "").strip()
        normalized_profile_name = str(profile_name or "").strip()
        if not normalized_activation_id and normalized_profile_name:
            normalized_activation_id = self._activation_by_profile.get(normalized_profile_name, "")
        if not normalized_activation_id:
            return

        tracker = self._trackers.pop(normalized_activation_id, None)
        if tracker is None:
            return
        current = self._activation_by_profile.get(tracker.profile_name)
        if current == tracker.activation_id:
            self._activation_by_profile.pop(tracker.profile_name, None)
        self._cancel_timeout(tracker)

    def reset(self) -> None:
        for tracker in list(self._trackers.values()):
            self._cancel_timeout(tracker)
        self._trackers.clear()
        self._activation_by_profile.clear()
        self._active_trigger_ids.clear()

    def observe_trigger_start(self, trigger_id: str | None) -> None:
        normalized = str(trigger_id or "").strip()
        if normalized:
            self._active_trigger_ids.add(normalized)

    def observe_trigger_end(self, trigger_id: str | None) -> None:
        normalized = str(trigger_id or "").strip()
        if not normalized:
            return
        self._active_trigger_ids.discard(normalized)
        for tracker in list(self._trackers.values()):
            if (
                tracker.trigger_id == normalized
                and tracker.deactivation.on_trigger_end
                and not tracker.expired
            ):
                self._expire(tracker, "trigger_end")

    def record_action(
        self,
        source_profile_name: str | None = None,
        trigger_id: str | None = None,
    ) -> None:
        del source_profile_name
        normalized_trigger_id = str(trigger_id or "").strip()
        for tracker in list(self._trackers.values()):
            if tracker.expired:
                continue
            if normalized_trigger_id and tracker.trigger_id == normalized_trigger_id:
                continue
            threshold = tracker.deactivation.after_actions
            if threshold is None or threshold <= 0:
                continue
            tracker.action_count += 1
            if tracker.action_count >= threshold:
                self._expire(tracker, "action_count")

    async def _timeout_after(
        self,
        tracker: RuntimeProfileActivationTracker,
        delay_s: float,
    ) -> None:
        try:
            await asyncio.sleep(max(0.001, delay_s))
            self._expire(tracker, "timeout")
        except asyncio.CancelledError:
            raise

    def _expire(
        self,
        tracker: RuntimeProfileActivationTracker,
        reason: str,
    ) -> None:
        current = self._trackers.get(tracker.activation_id)
        if current is not tracker or tracker.expired:
            return
        tracker.expired = True
        self.cancel(activation_id=tracker.activation_id)
        asyncio.create_task(self._broadcast_expiry(tracker, reason))

    async def _broadcast_expiry(
        self,
        tracker: RuntimeProfileActivationTracker,
        reason: str,
    ) -> None:
        self._broadcast_deactivate_request(
            {
                "profile_name": tracker.profile_name,
                "activation_id": tracker.activation_id,
                "reason": reason,
            }
        )

    def _cancel_timeout(self, tracker: RuntimeProfileActivationTracker) -> None:
        task = tracker.timeout_task
        if task is None or task.done():
            return
        if task is asyncio.current_task():
            return
        task.cancel()

        def _consume_cancelled(done: asyncio.Task[None]) -> None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                done.exception()

        task.add_done_callback(_consume_cancelled)
