from keymasq.gui.widgets.device_tab.commit import DeferredCommitState


def test_deferred_commit_state_chains_edits_in_order() -> None:
    state = DeferredCommitState()
    committed: list[str] = []

    state.queue(lambda: committed.append("first"))
    state.queue(lambda: committed.append("second"))
    commit = state.take()

    assert commit is not None
    commit()
    assert committed == ["first", "second"]
    assert state.pending is None


def test_deferred_commit_state_clears_source_independently() -> None:
    state = DeferredCommitState(source_id=42)

    assert state.clear_source() == 42
    assert state.source_id == 0
    assert state.clear_source() == 0
