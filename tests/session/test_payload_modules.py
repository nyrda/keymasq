from types import SimpleNamespace

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import AnalogControlConfig
from keymasq.common.model.core import (
    ActionType,
    SuperkeyMode,
)
from keymasq.common.model.superkeys import (
    SuperkeyAction,
    SuperkeyConfig,
)
from keymasq.session.manager.payload import analog, macro, references, superkey
from keymasq.session.manager.state import ExecRuntimeState


def test_macro_fields_can_be_built_without_a_session_manager() -> None:
    action = MappingAction(
        action_type=ActionType.MACRO,
        macro_name="paste",
        macro_replay_mouse_movement=False,
        macro_speed=1.5,
        macro_loop_mode="count",
        macro_loop_count=2,
    )

    inspector: dict[str, object] = {"action": "macro"}
    runtime: dict[str, object] = {"action": "macro"}

    macro.add_inspector_fields(inspector, action)
    assert macro.add_runtime_fields(runtime, action, include_empty=False) is True

    assert inspector["target"] == "paste"
    assert inspector["speed"] == 1.5
    assert inspector["loop_count"] == 2
    assert runtime["macro_name"] == "paste"
    assert runtime["macro_speed"] == 1.5
    assert runtime["macro_loop_count"] == 2


def test_analog_config_serialization_does_not_require_the_full_manager() -> None:
    config = AnalogControlConfig(name="Stick")

    payload = analog.serialize(SimpleNamespace(), config, "pad")

    assert payload["name"] == "Stick"
    assert payload["input_type"] == "stick"
    assert payload["thresholds"] == []


def test_superkey_expansion_does_not_require_the_full_manager() -> None:
    config = SuperkeyConfig(
        name="launcher",
        mode=SuperkeyMode.PATTERN,
        tap_actions=[SuperkeyAction(action_type=ActionType.KEYBOARD, target="key_space")],
    )

    payload = superkey.serialize(SimpleNamespace(), config, "kbd")

    assert payload["tap_actions"] == [{"action": "keyboard", "target": "key_space"}]


def test_command_reference_lifecycle_is_independently_testable() -> None:
    manager = SimpleNamespace(exec_state=ExecRuntimeState())

    device_ref = references.allocate(
        manager,
        "echo device",
        owner="device",
        hardware_id="kbd",
    )
    combo_ref = references.allocate(manager, "echo combo", owner="combo")

    assert (device_ref, combo_ref) == (1, 2)
    assert manager.exec_state.device_exec_refs == {"kbd": {1}}
    assert manager.exec_state.combo_exec_refs == {2}

    references.clear_device(manager, "kbd")
    assert set(manager.exec_state.exec_refs) == {2}
    assert manager.exec_state.exec_refs[2].cmd == "echo combo"

    references.clear_combos(manager)
    assert manager.exec_state.exec_refs == {}
