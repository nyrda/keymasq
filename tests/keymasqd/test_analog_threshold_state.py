from keymasq.common.model.analog import AnalogActionThreshold
from keymasq.keymasqd.runtime.analog.threshold_state import (
    DigitalThresholdStateMachine,
)


def _threshold(
    *,
    axis: str = "x",
    trigger_min: float = 0.7,
    trigger_max: float = 1.0,
    release_min: float = 0.5,
    release_max: float = 1.0,
) -> AnalogActionThreshold:
    return AnalogActionThreshold(
        axis=axis,
        trigger_min=trigger_min,
        trigger_max=trigger_max,
        release_min=release_min,
        release_max=release_max,
    )


def test_threshold_machine_applies_hysteresis_without_repeating_edges() -> None:
    active: set[str] = set()
    machine = DigitalThresholdStateMachine("left_stick", active)
    threshold = _threshold()

    transitions = list(
        machine.evaluate(
            [threshold],
            {"x": 0.8},
            input_type="stick",
        )
    )
    assert [(transition.kind, transition.index) for transition in transitions] == [("activate", 0)]
    assert active == {"left_stick:0"}

    assert list(machine.evaluate([threshold], {"x": 0.6}, input_type="stick")) == []
    assert active == {"left_stick:0"}

    transitions = list(
        machine.evaluate(
            [threshold],
            {"x": 0.4},
            input_type="stick",
        )
    )
    assert [(transition.kind, transition.index) for transition in transitions] == [("release", 0)]
    assert active == set()


def test_threshold_machine_tracks_overlapping_thresholds_independently() -> None:
    machine = DigitalThresholdStateMachine("stick", set())
    thresholds = [
        _threshold(trigger_min=0.5, release_min=0.4),
        _threshold(trigger_min=0.7, release_min=0.6),
    ]

    transitions = list(machine.evaluate(thresholds, {"x": 0.8}, input_type="stick"))
    assert [(transition.kind, transition.index) for transition in transitions] == [
        ("activate", 0),
        ("activate", 1),
    ]

    transitions = list(machine.evaluate(thresholds, {"x": 0.5}, input_type="stick"))
    assert [(transition.kind, transition.index) for transition in transitions] == [("release", 1)]


def test_axis_threshold_machine_reads_signed_normalized_value() -> None:
    machine = DigitalThresholdStateMachine("trigger", set())
    threshold = _threshold(trigger_min=-1.0, trigger_max=-0.5)

    transitions = list(
        machine.evaluate(
            [threshold],
            {"x": 0.9, "x_signed": -0.8},
            input_type="axis",
        )
    )

    assert [(transition.kind, transition.index) for transition in transitions] == [("activate", 0)]
