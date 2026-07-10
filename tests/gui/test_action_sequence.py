from keymasq.gui.widgets.action_sequence import OrderedActionState


def test_ordered_action_state_edits_without_widgets() -> None:
    state = OrderedActionState(["tap", "hold", "release"])

    assert state.move_up(1) == 0
    assert state.items == ["hold", "tap", "release"]
    assert state.move_down(1) == 2
    state.replace(1, "double")
    state.replace(0, None)

    snapshot = state.snapshot()
    snapshot.append("outside")
    assert state.items == ["double", "tap"]
