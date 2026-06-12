from typing import cast

import pytest

from keymasq.common.ipc import CommandType
from keymasq.common.security import SecurityPolicy
from keymasq.keymasqd import daemon as daemon_module
from keymasq.keymasqd import daemon_macro_commands
from tests.keymasqd.daemon_support import client_context, macro_meta


async def _resolve_macro_actions(macro_store, resolver_kind, actions):
    if resolver_kind == "mapping":
        source_ids = [f"source_{index}" for index in range(len(actions))]
        resolved = await daemon_macro_commands.resolve_mapping_macros(
            macro_store,
            dict(zip(source_ids, actions, strict=True)),
        )
        return [cast(dict[str, object], resolved[source_id]) for source_id in source_ids]

    resolved = await daemon_macro_commands.resolve_combo_macros(
        macro_store,
        [
            {
                "id": f"combo-{index}",
                "name": f"Combo {index}",
                "steps": [],
                "action": action,
            }
            for index, action in enumerate(actions)
        ],
    )
    return [cast(dict[str, object], combo["action"]) for combo in resolved]


@pytest.mark.asyncio
async def test_resolve_mapping_macros_loads_macro_definition(daemon_testbed):
    daemon, _device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed

    macro_store.get_meta.return_value = macro_meta()

    resolved = await daemon_macro_commands.resolve_mapping_macros(
        daemon.macro_store,
        {
            "btn_side": {
                "action": "macro",
                "macro_name": "combo",
            }
        }
    )

    action = cast(dict[str, object], resolved["btn_side"])
    assert "macro_events" not in action
    assert action["macro_loop_mode"] == "count"
    assert action["macro_loop_count"] == 3
    assert action["macro_loop_stop_behavior"] == "cancel_run"
    assert action["macro_move_to_start"] is True
    assert action["macro_start_x"] == 111
    assert action["macro_start_y"] == 222
    assert action["macro_block_mouse_movement"] is True


@pytest.mark.asyncio
async def test_resolve_mapping_macros_loads_macro_definition_inside_superkey(daemon_testbed):
    daemon, _device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed

    macro_store.get_meta.return_value = macro_meta()

    resolved = await daemon_macro_commands.resolve_mapping_macros(
        daemon.macro_store,
        {
            "btn_side": {
                "action": "superkey",
                "superkey": {
                    "name": "demo",
                    "mode": "pattern",
                    "hold_actions": [{"action": "macro", "macro_name": "combo"}],
                },
            }
        },
    )

    action = cast(dict[str, object], resolved["btn_side"])
    superkey = cast(dict[str, object], action["superkey"])
    hold_action = cast(dict[str, object], cast(list[object], superkey["hold_actions"])[0])
    assert "macro_events" not in hold_action
    assert hold_action["macro_loop_mode"] == "count"
    assert hold_action["macro_loop_count"] == 3
    assert hold_action["macro_loop_stop_behavior"] == "cancel_run"
    assert hold_action["macro_move_to_start"] is True
    assert hold_action["macro_start_x"] == 111
    assert hold_action["macro_start_y"] == 222
    assert hold_action["macro_block_mouse_movement"] is True


@pytest.mark.asyncio
async def test_resolve_mapping_macros_traverses_all_nested_action_containers(
    daemon_testbed,
):
    daemon, _device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed
    superkey_action_keys = (
        "tap_actions",
        "double_tap_actions",
        "hold_actions",
        "tap_hold_actions",
        "overload_actions",
        "overload_down_actions",
        "overload_up_actions",
    )
    macro_names = [f"superkey_{key}" for key in superkey_action_keys] + [
        "analog_threshold"
    ]
    loop_counts = {name: index + 1 for index, name in enumerate(macro_names)}

    macro_store.get_meta.side_effect = lambda name: macro_meta(
        loop_count=loop_counts[str(name)],
        move_to_start=False,
        start_x=0,
        start_y=0,
        block_mouse_movement=False,
    )

    resolved = await daemon_macro_commands.resolve_mapping_macros(
        daemon.macro_store,
        {
            "btn_side": {
                "action": "superkey",
                "superkey": {
                    key: [
                        {
                            "action": "macro",
                            "macro_name": f"superkey_{key}",
                        }
                    ]
                    for key in superkey_action_keys
                },
            },
            "left_stick": {
                "action": "analog_control",
                "analog_control": {
                    "thresholds": [
                        {
                            "actions": [
                                {
                                    "action": "macro",
                                    "macro_name": "analog_threshold",
                                }
                            ]
                        }
                    ]
                },
            },
        },
    )

    loaded_names = [call.args[0] for call in macro_store.get_meta.call_args_list]
    assert loaded_names == sorted(macro_names)

    action = cast(dict[str, object], resolved["btn_side"])
    superkey = cast(dict[str, object], action["superkey"])
    for key in superkey_action_keys:
        actions = cast(list[object], superkey[key])
        nested = cast(dict[str, object], actions[0])
        assert nested["macro_loop_count"] == loop_counts[f"superkey_{key}"]

    analog_action = cast(dict[str, object], resolved["left_stick"])
    analog_control = cast(dict[str, object], analog_action["analog_control"])
    threshold = cast(dict[str, object], cast(list[object], analog_control["thresholds"])[0])
    threshold_action = cast(dict[str, object], cast(list[object], threshold["actions"])[0])
    assert threshold_action["macro_loop_count"] == loop_counts["analog_threshold"]


@pytest.mark.asyncio
async def test_mapping_and_combo_macro_resolution_match_for_nested_actions(daemon_testbed):
    daemon, _device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed
    macro_store.get_meta.return_value = macro_meta(
        loop_count=6,
        start_x=11,
        start_y=12,
    )

    def nested_action():
        return {
            "action": "analog_control",
            "analog_control": {
                "thresholds": [
                    {
                        "actions": [
                            {
                                "action": "macro",
                                "macro_name": "combo",
                            }
                        ]
                    }
                ]
            },
        }

    mapping_resolved = await daemon_macro_commands.resolve_mapping_macros(
        daemon.macro_store,
        {"left_stick": nested_action()},
    )
    combo_resolved = await daemon_macro_commands.resolve_combo_macros(
        daemon.macro_store,
        [{"id": "combo-1", "name": "Combo", "steps": [], "action": nested_action()}],
    )

    mapping_action = cast(dict[str, object], mapping_resolved["left_stick"])
    combo_action = cast(dict[str, object], combo_resolved[0]["action"])
    assert mapping_action == combo_action

    analog_control = cast(dict[str, object], mapping_action["analog_control"])
    threshold = cast(dict[str, object], cast(list[object], analog_control["thresholds"])[0])
    threshold_action = cast(dict[str, object], cast(list[object], threshold["actions"])[0])
    assert threshold_action["macro_loop_mode"] == "count"
    assert threshold_action["macro_loop_count"] == 6
    assert threshold_action["macro_loop_stop_behavior"] == "cancel_run"


@pytest.mark.parametrize("resolver_kind", ["mapping", "combo"])
@pytest.mark.asyncio
async def test_resolve_macros_deduplicates_macro_store_reads(
    daemon_testbed,
    resolver_kind,
):
    daemon, _device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed

    macro_store.get_meta.side_effect = lambda name: macro_meta(
        loop_count=3 if name == "combo" else 2,
        move_to_start=False,
        start_x=0,
        start_y=0,
        block_mouse_movement=False,
    )

    resolved_actions = await _resolve_macro_actions(
        daemon.macro_store,
        resolver_kind,
        [
            {"action": "macro", "macro_name": "combo"},
            {"action": "macro", "macro_name": "combo"},
            {"action": "macro", "macro_name": "other"},
        ],
    )

    side, extra, middle = resolved_actions
    assert side["macro_loop_count"] == 3
    assert extra["macro_loop_count"] == 3
    assert middle["macro_loop_count"] == 2
    assert macro_store.get_meta.call_count == 2


@pytest.mark.parametrize("resolver_kind", ["mapping", "combo"])
@pytest.mark.asyncio
async def test_resolve_macros_ignores_malformed_stored_macro_values(
    daemon_testbed,
    resolver_kind,
):
    daemon, _device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed

    macro_store.get_meta.return_value = {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "count",
        "loop_count": "",
        "move_to_start": True,
        "start_x": "abc",
        "start_y": 0,
        "block_mouse_movement": False,
    }

    resolved_actions = await _resolve_macro_actions(
        daemon.macro_store,
        resolver_kind,
        [
            {"action": "macro", "macro_name": "broken"},
            {"action": "keyboard", "target": "key_a"},
        ],
    )

    assert resolved_actions[0] == {"action": "macro", "macro_name": "broken"}
    assert resolved_actions[1] == {"action": "keyboard", "target": "key_a"}


@pytest.mark.asyncio
async def test_handle_command_set_mapping_resolves_macro_values(daemon_testbed):
    daemon, device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(recording_unlock_required=False)
    macro_store.get_meta.return_value = macro_meta(
        loop_count=2,
        move_to_start=False,
        start_x=4,
        start_y=5,
        block_mouse_movement=False,
    )

    await daemon._handle_command(
        CommandType.SET_MAPPING,
        {
            "hardware_id": "123",
            "mapping": {"btn_side": {"action": "macro", "macro_name": "combo"}},
        },
    )

    sent_mapping = cast(
        dict[str, dict[str, object]],
        device_manager.set_mapping.await_args.kwargs["mapping"],
    )
    resolved = sent_mapping["btn_side"]
    assert "macro_events" not in resolved
    assert resolved["macro_loop_mode"] == "count"
    assert resolved["macro_loop_count"] == 2


@pytest.mark.asyncio
async def test_handle_command_set_combos_resolves_macro_values(daemon_testbed):
    daemon, device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(recording_unlock_required=False)
    macro_store.get_meta.return_value = macro_meta(
        loop_count=4,
        start_x=7,
        start_y=8,
    )

    await daemon._handle_command(
        CommandType.SET_COMBOS,
        {
            "combos": [
                {
                    "id": "combo-1",
                    "name": "Combo",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "mouse",
                                    "evdev": "btn_side",
                                }
                            ]
                        }
                    ],
                    "action": {"action": "macro", "macro_name": "combo"},
                }
            ]
        },
    )

    sent_combos = cast(list[dict[str, object]], device_manager.set_combos.await_args.args[0])
    first_action = cast(dict[str, object], sent_combos[0]["action"])
    assert "macro_events" not in first_action
    assert first_action["macro_loop_mode"] == "count"
    assert first_action["macro_loop_count"] == 4


@pytest.mark.asyncio
async def test_handle_command_set_combos_resolves_macro_values_inside_superkey(daemon_testbed):
    daemon, device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(recording_unlock_required=False)
    macro_store.get_meta.return_value = macro_meta(
        loop_mode="hold",
        loop_count=5,
        start_x=7,
        start_y=8,
    )

    await daemon._handle_command(
        CommandType.SET_COMBOS,
        {
            "combos": [
                {
                    "id": "combo-1",
                    "name": "Combo",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "mouse",
                                    "evdev": "btn_side",
                                }
                            ]
                        }
                    ],
                    "action": {
                        "action": "superkey",
                        "superkey": {
                            "name": "demo",
                            "mode": "pattern",
                            "hold_actions": [{"action": "macro", "macro_name": "combo"}],
                        },
                    },
                }
            ]
        },
    )

    sent_combos = cast(list[dict[str, object]], device_manager.set_combos.await_args.args[0])
    combo_action = cast(dict[str, object], sent_combos[0]["action"])
    superkey = cast(dict[str, object], combo_action["superkey"])
    hold_action = cast(dict[str, object], cast(list[object], superkey["hold_actions"])[0])
    assert "macro_events" not in hold_action
    assert hold_action["macro_loop_mode"] == "hold"
    assert hold_action["macro_loop_count"] == 5
    assert hold_action["macro_loop_stop_behavior"] == "cancel_run"
    assert hold_action["macro_move_to_start"] is True
    assert hold_action["macro_start_x"] == 7
    assert hold_action["macro_start_y"] == 8
    assert hold_action["macro_block_mouse_movement"] is True


@pytest.mark.asyncio
async def test_handle_command_start_recording_requires_macro_recording_opt_in(
    daemon_testbed,
    monkeypatch,
):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(recording_unlock_required=True)

    monkeypatch.setattr(
        daemon_module,
        "resolve_macro_recording_status",
        lambda _uid: {"unlocked": False, "source": "none", "expires_at": 0},
    )

    with pytest.raises(PermissionError, match="macro_recording_disabled"):
        await daemon._handle_command(
            CommandType.START_RECORDING,
            {},
            client=client_context(uid=1000, pid=111, connection_id=7),
        )


def test_macro_recording_enabled_cache_rechecks_persistent_opt_in(
    daemon_testbed,
    monkeypatch,
):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    now_mono = 100.0
    statuses = [
        {"unlocked": True, "source": "persistent", "expires_at": 0},
        {"unlocked": False, "source": "none", "expires_at": 0},
    ]
    calls: list[int] = []

    def resolve_status(_uid: int) -> dict[str, bool | int | str]:
        calls.append(_uid)
        return statuses[min(len(calls) - 1, len(statuses) - 1)]

    monkeypatch.setattr(daemon_module, "resolve_macro_recording_status", resolve_status)
    monkeypatch.setattr(daemon_module.time, "time", lambda: 1000)
    monkeypatch.setattr(daemon_module.time, "monotonic", lambda: now_mono)

    assert daemon._macro_recording_enabled_for_uid(1000) == (True, 0, "persistent")

    now_mono = 100.5
    assert daemon._macro_recording_enabled_for_uid(1000) == (True, 0, "persistent")

    now_mono = 102.0
    assert daemon._macro_recording_enabled_for_uid(1000) == (False, 0, "none")
    assert calls == [1000, 1000]


def test_macro_recording_enabled_cache_rechecks_disabled_opt_in(
    daemon_testbed,
    monkeypatch,
):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    statuses = [
        {"unlocked": False, "source": "none", "expires_at": 0},
        {"unlocked": True, "source": "persistent", "expires_at": 0},
    ]
    calls: list[int] = []

    def resolve_status(uid: int) -> dict[str, bool | int | str]:
        calls.append(uid)
        return statuses[min(len(calls) - 1, len(statuses) - 1)]

    monkeypatch.setattr(daemon_module, "resolve_macro_recording_status", resolve_status)
    monkeypatch.setattr(daemon_module.time, "time", lambda: 1000)
    monkeypatch.setattr(daemon_module.time, "monotonic", lambda: 100.0)

    assert daemon._macro_recording_enabled_for_uid(1000) == (False, 0, "none")
    assert daemon._macro_recording_enabled_for_uid(1000) == (True, 0, "persistent")
    assert calls == [1000, 1000]
