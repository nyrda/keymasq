from dataclasses import replace

import pytest

from keymasq.common.model.analog import (
    SAME_DEVICE_OUTPUT_ID,
    AnalogActionThreshold,
    AnalogControlConfig,
    AnalogGamepadOutputConfig,
    AnalogMouseMotionConfig,
)
from keymasq.gui.widgets.analog_control.draft import ControlDraft
from keymasq.gui.widgets.analog_control.persistence import AnalogControlPersistence
from keymasq.gui.widgets.analog_control.thresholds import ThresholdState


def test_control_draft_round_trips_area_mode_and_tick_interval() -> None:
    config = AnalogControlConfig(
        name="Area",
        description="description",
        mouse_motion=AnalogMouseMotionConfig(
            enabled=True,
            mode="area",
            area_radius_x=640,
            area_radius_y=480,
            tick_ms=12,
        ),
    )

    draft = ControlDraft.from_config(config)
    restored = draft.to_config()

    assert draft.mode == "mouse_area"
    assert restored.mouse_motion.enabled is True
    assert restored.mouse_motion.mode == "area"
    assert restored.mouse_motion.area_radius_x == 640
    assert restored.mouse_motion.tick_ms == 12


def test_control_draft_applies_axis_gamepad_output_policy() -> None:
    draft = ControlDraft.from_config(
        AnalogControlConfig(
            name="Axis",
            input_type="axis",
            gamepad_output=AnalogGamepadOutputConfig(
                enabled=True,
                output_id=SAME_DEVICE_OUTPUT_ID,
                output_direction="both",
                output_invert=True,
                output_invert_x=True,
                output_invert_y=True,
            ),
        )
    )

    config = draft.to_config()

    assert config.gamepad_output.output_direction == "both"
    assert config.gamepad_output.output_invert is True
    assert config.gamepad_output.output_invert_x is False
    assert config.gamepad_output.output_invert_y is False


def test_control_draft_serializes_only_the_selected_mode() -> None:
    threshold = AnalogActionThreshold("x", 0.5, 1.0, 0.4, 1.0)
    draft = ControlDraft.from_config(AnalogControlConfig(name="Digital"))
    draft = replace(draft, mode="gamepad", thresholds=(threshold,))

    config = draft.to_config()

    assert config.gamepad_output.enabled is True
    assert config.mouse_motion.enabled is False
    assert config.thresholds == []


def test_control_draft_detects_pristine_new_editor_values() -> None:
    draft = ControlDraft.new()

    assert draft.is_pristine_new_control() is True
    assert replace(draft, description="changed").is_pristine_new_control() is False
    with pytest.raises(ValueError, match="name is required"):
        replace(draft, name="  ").to_config()


def test_threshold_state_normalizes_axis_ranges_without_gtk() -> None:
    state = ThresholdState(
        items=[AnalogActionThreshold("y", 0.8, -0.2, 0.9, -0.4)],
    )

    state.sync_input_type(axis_control=True, domain=(-1.0, 1.0))

    threshold = state.items[0]
    assert threshold.axis == "x"
    assert (threshold.trigger_min, threshold.trigger_max) == (-0.2, 0.8)
    assert threshold.release_min <= threshold.trigger_min
    assert threshold.release_max >= threshold.trigger_max


def test_threshold_state_primary_and_advanced_transitions_envelop_activation() -> None:
    state = ThresholdState()
    state.add_range()

    threshold = state.update_primary(
        0,
        trigger_min=0.9,
        trigger_max=0.4,
        hysteresis=0.2,
        domain=(-1.0, 1.0),
    )
    assert threshold is not None
    assert (threshold.trigger_min, threshold.trigger_max) == (0.4, 0.9)
    assert (threshold.release_min, threshold.release_max) == (0.2, 1.0)

    threshold = state.update_advanced(0, release_min=0.8, release_max=0.5)
    assert threshold is not None
    assert threshold.release_min == threshold.trigger_min
    assert threshold.release_max == threshold.trigger_max


class _Store:
    def __init__(self, *, delete_result: bool = True) -> None:
        self.delete_result = delete_result
        self.saved: list[tuple[str, str | None]] = []
        self.deleted: list[str] = []

    def save_analog_control(
        self,
        config: AnalogControlConfig,
        *,
        replacing_name: str | None = None,
    ) -> None:
        self.saved.append((config.name, replacing_name))

    def delete_analog_control(self, name: str) -> bool:
        self.deleted.append(name)
        return self.delete_result


class _Profiles:
    def __init__(self) -> None:
        self.renamed: list[tuple[str, str]] = []
        self.replaced: list[str] = []

    def rename_analog_control_references(self, old_name: str, new_name: str) -> None:
        self.renamed.append((old_name, new_name))

    def replace_analog_control_with_suppress(self, name: str) -> None:
        self.replaced.append(name)


def test_persistence_coordinates_profile_references_after_store_changes() -> None:
    store = _Store()
    profiles = _Profiles()
    persistence = AnalogControlPersistence(store)

    persistence.save(
        AnalogControlConfig(name="New"),
        replacing_name="Old",
        profiles=profiles,
    )
    assert persistence.delete("New", profiles=profiles) is True

    assert store.saved == [("New", "Old")]
    assert profiles.renamed == [("Old", "New")]
    assert profiles.replaced == ["New"]


def test_failed_delete_does_not_change_profile_references() -> None:
    store = _Store(delete_result=False)
    profiles = _Profiles()

    assert AnalogControlPersistence(store).delete("Keep", profiles=profiles) is False
    assert profiles.replaced == []
