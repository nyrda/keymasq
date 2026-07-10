import asyncio

import pytest

from keymasq.session.manager.profile.reconciliation import (
    ReconciliationGenerationState,
)
from keymasq.session.manager.state import ProfileRuntimeState


@pytest.mark.asyncio
async def test_reconciliation_generation_supersedes_previous_apply() -> None:
    runtime_state = ProfileRuntimeState()
    release_previous = asyncio.Event()
    previous = asyncio.create_task(release_previous.wait())
    runtime_state.apply_task = previous
    generations = ReconciliationGenerationState(runtime_state)

    generation = generations.begin("window changed", current=asyncio.current_task())
    await asyncio.sleep(0)

    assert generation == 1
    assert previous.cancelled()
    assert runtime_state.apply_reason == "window changed"


def test_reconciliation_generation_rejects_only_stale_work() -> None:
    runtime_state = ProfileRuntimeState(apply_generation=3)
    generations = ReconciliationGenerationState(runtime_state)

    generations.ensure_current(None)
    generations.ensure_current(3)
    with pytest.raises(asyncio.CancelledError):
        generations.ensure_current(2)


@pytest.mark.asyncio
async def test_reconciliation_generation_clears_only_tracked_task() -> None:
    runtime_state = ProfileRuntimeState()
    generations = ReconciliationGenerationState(runtime_state)
    first = asyncio.create_task(asyncio.sleep(0))
    second = asyncio.create_task(asyncio.sleep(0))
    generations.track(second)

    generations.clear_if_current(first)
    assert runtime_state.apply_task is second

    generations.clear_if_current(second)
    assert runtime_state.apply_task is None
    await asyncio.gather(first, second)
