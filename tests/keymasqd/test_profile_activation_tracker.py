import asyncio

import pytest

from keymasq.common.models import ProfileDeactivationPolicy
from keymasq.keymasqd.runtime.profile_activation_tracker import ProfileActivationTracker


@pytest.mark.asyncio
async def test_track_same_activation_id_cancels_replaced_timeout_task() -> None:
    broadcasts: list[dict[str, object]] = []
    tracker = ProfileActivationTracker(
        broadcast_deactivate_request=broadcasts.append,
    )
    policy = ProfileDeactivationPolicy(timeout_ms=60_000)

    tracker.track(
        profile_name="Nav",
        activation_id="activation-1",
        trigger_id="trigger-1",
        deactivation=policy,
    )
    first_tracker = tracker._trackers["activation-1"]
    first_task = first_tracker.timeout_task
    assert first_task is not None

    tracker.track(
        profile_name="Nav",
        activation_id="activation-1",
        trigger_id="trigger-2",
        deactivation=policy,
    )
    await asyncio.sleep(0)

    latest_tracker = tracker._trackers["activation-1"]
    assert latest_tracker is not first_tracker
    assert first_task.cancelled()

    tracker._expire(first_tracker, "timeout")
    await asyncio.sleep(0)
    assert broadcasts == []

    tracker._expire(latest_tracker, "timeout")
    await asyncio.sleep(0)
    assert broadcasts == [
        {
            "profile_name": "Nav",
            "activation_id": "activation-1",
            "reason": "timeout",
        }
    ]
