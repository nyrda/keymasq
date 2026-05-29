# ruff: noqa: F403, F405, I001
from tests.keymasqd.daemon_support import *

@pytest.mark.asyncio
async def test_resolve_mapping_macros_loads_macro_definition(daemon_testbed):
    daemon, _device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed

    macro_store.get_meta.return_value = {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "count",
        "loop_count": 3,
        "loop_stop_behavior": "cancel_run",
        "move_to_start": True,
        "start_x": 111,
        "start_y": 222,
        "block_mouse_movement": True,
    }

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

    macro_store.get_meta.return_value = {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "count",
        "loop_count": 3,
        "loop_stop_behavior": "cancel_run",
        "move_to_start": True,
        "start_x": 111,
        "start_y": 222,
        "block_mouse_movement": True,
    }

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

    macro_store.get_meta.side_effect = lambda name: {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "count",
        "loop_count": loop_counts[str(name)],
        "move_to_start": False,
        "start_x": 0,
        "start_y": 0,
        "block_mouse_movement": False,
    }

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
async def test_resolve_mapping_macros_deduplicates_macro_store_reads(daemon_testbed):
    daemon, _device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed

    macro_store.get_meta.side_effect = lambda name: {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "count",
        "loop_count": 3 if name == "combo" else 2,
        "move_to_start": False,
        "start_x": 0,
        "start_y": 0,
        "block_mouse_movement": False,
    }

    resolved = await daemon_macro_commands.resolve_mapping_macros(
        daemon.macro_store,
        {
            "btn_side": {"action": "macro", "macro_name": "combo"},
            "btn_extra": {"action": "macro", "macro_name": "combo"},
            "btn_middle": {"action": "macro", "macro_name": "other"},
        }
    )

    side = cast(dict[str, object], resolved["btn_side"])
    extra = cast(dict[str, object], resolved["btn_extra"])
    middle = cast(dict[str, object], resolved["btn_middle"])
    assert side["macro_loop_count"] == 3
    assert extra["macro_loop_count"] == 3
    assert middle["macro_loop_count"] == 2
    assert macro_store.get_meta.call_count == 2


@pytest.mark.asyncio
async def test_resolve_mapping_macros_ignores_malformed_stored_macro_values(daemon_testbed):
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

    resolved = await daemon_macro_commands.resolve_mapping_macros(
        daemon.macro_store,
        {
            "btn_side": {"action": "macro", "macro_name": "broken"},
            "btn_middle": {"action": "keyboard", "target": "key_a"},
        }
    )

    assert resolved["btn_side"] == {"action": "macro", "macro_name": "broken"}
    assert resolved["btn_middle"] == {"action": "keyboard", "target": "key_a"}


@pytest.mark.asyncio
async def test_handle_command_set_mapping_resolves_macro_values(daemon_testbed):
    daemon, device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(recording_unlock_required=False)
    macro_store.get_meta.return_value = {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "count",
        "loop_count": 2,
        "move_to_start": False,
        "start_x": 4,
        "start_y": 5,
        "block_mouse_movement": False,
    }

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
    macro_store.get_meta.return_value = {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "count",
        "loop_count": 4,
        "move_to_start": True,
        "start_x": 7,
        "start_y": 8,
        "block_mouse_movement": True,
    }

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
    macro_store.get_meta.return_value = {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "hold",
        "loop_count": 5,
        "loop_stop_behavior": "cancel_run",
        "move_to_start": True,
        "start_x": 7,
        "start_y": 8,
        "block_mouse_movement": True,
    }

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
async def test_resolve_combo_macros_deduplicates_macro_store_reads(daemon_testbed):
    daemon, _device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed

    macro_store.get_meta.side_effect = lambda name: {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "count",
        "loop_count": 4 if name == "combo" else 1,
        "move_to_start": False,
        "start_x": 0,
        "start_y": 0,
        "block_mouse_movement": False,
    }

    resolved = await daemon_macro_commands.resolve_combo_macros(
        daemon.macro_store,
        [
            {
                "id": "combo-1",
                "name": "First",
                "steps": [],
                "action": {"action": "macro", "macro_name": "combo"},
            },
            {
                "id": "combo-2",
                "name": "Second",
                "steps": [],
                "action": {"action": "macro", "macro_name": "combo"},
            },
            {
                "id": "combo-3",
                "name": "Third",
                "steps": [],
                "action": {"action": "macro", "macro_name": "other"},
            },
        ]
    )

    first = cast(dict[str, object], resolved[0]["action"])
    second = cast(dict[str, object], resolved[1]["action"])
    third = cast(dict[str, object], resolved[2]["action"])
    assert first["macro_loop_count"] == 4
    assert second["macro_loop_count"] == 4
    assert third["macro_loop_count"] == 1
    assert macro_store.get_meta.call_count == 2


@pytest.mark.asyncio
async def test_resolve_combo_macros_ignores_malformed_stored_macro_values(daemon_testbed):
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

    resolved = await daemon_macro_commands.resolve_combo_macros(
        daemon.macro_store,
        [
            {
                "id": "combo-1",
                "name": "Broken",
                "steps": [],
                "action": {"action": "macro", "macro_name": "broken"},
            },
            {
                "id": "combo-2",
                "name": "Keyboard",
                "steps": [],
                "action": {"action": "keyboard", "target": "key_f5"},
            },
        ]
    )

    assert resolved[0]["action"] == {"action": "macro", "macro_name": "broken"}
    assert resolved[1]["action"] == {"action": "keyboard", "target": "key_f5"}


@pytest.mark.asyncio
async def test_handle_command_start_recording_respects_runtime_lock(daemon_testbed, monkeypatch):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(recording_unlock_required=True)

    monkeypatch.setattr(
        daemon_module,
        "resolve_unlock_status",
        lambda _uid: {"unlocked": False, "source": "none", "expires_at": 0},
    )

    with pytest.raises(PermissionError, match="recording_locked"):
        await daemon._handle_command(
            CommandType.START_RECORDING,
            {},
            client=_client(uid=1000, pid=111, connection_id=7),
        )
