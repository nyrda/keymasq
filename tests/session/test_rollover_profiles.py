import asyncio
import re
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from keymasq.common.ipc import CommandType, Response
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType, DeviceType
from keymasq.common.model.hardware import ButtonDefinition, EvdevDevice, HardwareConfig
from keymasq.common.model.profiles import (
    DeviceProfileLayer,
    ProfileConfig,
    RolloverGroup,
    RolloverMember,
    RolloverWinner,
)
from keymasq.session.manager.core import SessionManager
from keymasq.session.manager.payload import rollover as rollover_payload
from keymasq.session.manager.profile import application, coordinator
from keymasq.session.manager.profile.grab_plan import get_interfaces_to_grab
from keymasq.session.manager.profile.runtime_state import invalidate_grabbed_state
from keymasq.session.profile import references
from keymasq.session.profile.codec import ProfileCodec
from keymasq.session.profile.resolution import ProfileResolver
from keymasq.session.profile.types import ProfileInfo, ResolvedDeviceProfile

KEYBOARD = "1234:5678"
MOUSE = "046d:c52b"


def kb(button: str) -> RolloverMember:
    return RolloverMember(hardware_id=KEYBOARD, button=button)


def side() -> RolloverMember:
    return RolloverMember(hardware_id=MOUSE, button="btn_side")


def _profile(name: str, priority: int, groups: list[RolloverGroup]) -> ProfileConfig:
    return ProfileConfig(
        name=name,
        enabled=True,
        is_permanent=True,
        priority=priority,
        created_at=datetime(2026, 1, 1),
        device_layers={KEYBOARD: DeviceProfileLayer(hardware_id=KEYBOARD)},
        rollover_groups=groups,
    )


def _hardware() -> HardwareConfig:
    return HardwareConfig(
        "1234",
        "5678",
        "Split keyboard",
        [
            EvdevDevice("/dev/input/event0", DeviceType.KEYBOARD, "kbd"),
            EvdevDevice("/dev/input/event1", DeviceType.KEYBOARD, "media"),
        ],
        [
            ButtonDefinition(id="key_a", label="A", evdev="key_a", source="kbd"),
            ButtonDefinition(id="key_d", label="D", evdev="key_d", source="kbd"),
            ButtonDefinition(id="key_play", label="Play", evdev="key_playpause", source="media"),
        ],
    )


def test_codec_round_trips_rollover_groups() -> None:
    codec = ProfileCodec()
    original = _profile(
        "Strafe",
        0,
        [
            RolloverGroup(
                name="A / Side",
                members=[kb("key_a"), side()],
                winner=RolloverWinner.PRIORITY,
                restore=False,
            )
        ],
    )

    data = codec.encode(original)
    assert data["rollover_groups"] == [
        {
            "name": "A / Side",
            "members": [
                {"hardware_id": KEYBOARD, "button": "key_a"},
                {"hardware_id": MOUSE, "button": "btn_side"},
            ],
            "winner": "priority",
            "restore": False,
        }
    ]
    devices = data["devices"]
    assert isinstance(devices, dict)
    assert "rollover_groups" not in devices[KEYBOARD]

    decoded = codec.decode(data, default_name="fallback").config
    assert decoded.rollover_groups == original.rollover_groups


def _member(button: str) -> dict[str, str]:
    return {"hardware_id": KEYBOARD, "button": button}


def test_codec_omits_empty_groups() -> None:
    codec = ProfileCodec()

    assert "rollover_groups" not in codec.encode(_profile("Plain", 0, []))


@pytest.mark.parametrize(
    ("groups", "message"),
    [
        (
            [{"name": "AD", "members": [_member("key_a"), {"hardware_id": KEYBOARD, "btn": "d"}]}],
            "rollover group 'AD' member 2 needs a hardware_id and a button",
        ),
        ([{"members": [_member("key_a")]}], "rollover group 1 needs at least 2 members"),
        ([{"name": "AD"}], "rollover group 'AD' needs a members array"),
        (
            [{"name": "AA", "members": [_member("key_a"), _member("key_a")]}],
            f"rollover group 'AA' lists {KEYBOARD} key_a twice",
        ),
        (
            [
                {"name": "AD", "members": [_member("key_a"), _member("key_d")]},
                {"name": "DW", "members": [_member("key_d"), _member("key_w")]},
            ],
            f"{KEYBOARD} key_d is in both rollover group 'AD' and rollover group 'DW'",
        ),
        (
            [{"name": "AD", "members": [_member("key_a"), _member("key_d")], "winner": "last"}],
            "rollover group 'AD' winner must be",
        ),
        (
            [{"name": "AD", "members": [_member("key_a"), _member("key_d")], "restore": "no"}],
            "rollover group 'AD' restore must be true or false",
        ),
        ([["key_a", "key_d"]], "rollover group 1 must be a table"),
        ({"name": "AD"}, "rollover_groups must be an array of tables"),
    ],
)
def test_codec_rejects_malformed_groups(groups: object, message: str) -> None:
    data = {
        "profile": {"name": "Hand edited", "created_at": "2026-01-01T00:00:00"},
        "rollover_groups": groups,
    }

    with pytest.raises(ValueError, match=re.escape(message)):
        ProfileCodec().decode(data, default_name="fallback")


def test_resolver_layers_groups_across_profiles_and_marks_member_devices() -> None:
    base = _profile(
        "Base",
        0,
        [
            RolloverGroup(name="strafe", members=[kb("key_a"), kb("key_d")]),
            RolloverGroup(name="walk", members=[kb("key_w"), kb("key_s")]),
        ],
    )
    overlay = _profile(
        "Overlay",
        10,
        [
            RolloverGroup(
                name="fighting",
                members=[kb("key_d"), side()],
                winner=RolloverWinner.NEUTRAL,
            )
        ],
    )
    profiles = {
        info.config.name: info
        for info in (
            ProfileInfo(Path("base.toml"), base),
            ProfileInfo(Path("overlay.toml"), overlay),
        )
    }

    resolved = ProfileResolver(profiles).resolve(hardware_ids=[KEYBOARD])

    assert [group.name for group in resolved.rollover_groups] == ["walk", "fighting"]
    assert resolved.devices[KEYBOARD].rollover_buttons == {"key_w", "key_s", "key_d"}
    mouse = resolved.devices[MOUSE]
    assert mouse.rollover_buttons == {"btn_side"}
    assert mouse.has_effective_mapping is True


def test_removing_a_control_or_device_drops_it_from_groups() -> None:
    config = _profile(
        "Strafe",
        0,
        [
            RolloverGroup(name="strafe", members=[kb("key_a"), side()]),
            RolloverGroup(name="wasd", members=[kb("key_w"), kb("key_s"), kb("key_x")]),
        ],
    )

    first = references.remove_button_mapping(config, KEYBOARD, "key_a")
    assert first.config is not None
    assert first.config.rollover_groups == [
        RolloverGroup(name="wasd", members=[kb("key_w"), kb("key_s"), kb("key_x")])
    ]

    second = references.remove_button_mapping(first.config, KEYBOARD, "key_x")
    assert second.config is not None
    assert second.config.rollover_groups == [
        RolloverGroup(name="wasd", members=[kb("key_w"), kb("key_s")])
    ]

    assert references.remove_button_mapping(second.config, KEYBOARD, "key_q").config is None

    removed = references.remove_device_layer(config, MOUSE)
    assert removed.config is not None
    assert [group.name for group in removed.config.rollover_groups] == ["wasd"]
    assert references.remove_device_layer(removed.config, MOUSE).config is None


def test_grab_plan_includes_interfaces_of_unmapped_group_members() -> None:
    resolved = ResolvedDeviceProfile(KEYBOARD, rollover_buttons={"key_a", "key_d"})

    interfaces = get_interfaces_to_grab(_hardware(), resolved, manager=Mock())

    assert interfaces == {"kbd": "/dev/input/event0"}


def _group(winner: RolloverWinner = RolloverWinner.OLDEST) -> RolloverGroup:
    return RolloverGroup(name="strafe", members=[kb("key_a"), side()], winner=winner)


@pytest.mark.asyncio
async def test_groups_are_sent_once_until_they_change() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=Response(status="ok", data={}))

    await application.update_rollover_groups(manager, [])
    assert manager.client.send_command.await_count == 0

    await application.update_rollover_groups(manager, [_group()])
    await application.update_rollover_groups(manager, [_group()])
    await application.update_rollover_groups(manager, [_group(RolloverWinner.NEUTRAL)])
    await application.update_rollover_groups(manager, [])

    sent = [call.args[0] for call in manager.client.send_command.await_args_list]
    assert [command.command for command in sent] == [CommandType.SET_ROLLOVER_GROUPS] * 3
    assert sent[0].data == {
        "groups": [
            {
                "name": "strafe",
                "members": [
                    {"hardware_id": KEYBOARD, "button": "key_a"},
                    {"hardware_id": MOUSE, "button": "btn_side"},
                ],
                "winner": "oldest",
                "restore": True,
            }
        ]
    }
    assert sent[2].data == {"groups": []}
    assert manager.profile_state.last_sent_rollover_signature == rollover_payload.EMPTY_SIGNATURE


@pytest.mark.asyncio
async def test_failed_or_disconnected_sends_are_retried() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(
        side_effect=[
            Response(status="error", error="daemon busy"),
            Response(status="ok", data={}),
            Response(status="ok", data={}),
        ]
    )

    await application.update_rollover_groups(manager, [_group()])
    await application.update_rollover_groups(manager, [_group()])
    assert manager.client.send_command.await_count == 2

    invalidate_grabbed_state(manager)
    await application.update_rollover_groups(manager, [_group()])
    assert manager.client.send_command.await_count == 3


@pytest.mark.asyncio
async def test_apply_keeps_groups_out_of_the_mapping_command() -> None:
    manager = SessionManager()
    hardware = _hardware()
    manager.hardware.get_hardware = lambda _hardware_id: hardware  # type: ignore[assignment]
    manager.client.send_command = AsyncMock(
        side_effect=[
            Response(status="ok", data={"grabbed_count": 1}),
            Response(status="ok", data={"updated": True}),
        ]
    )
    resolved = ResolvedDeviceProfile(
        KEYBOARD,
        active_profile_names=["Strafe"],
        mappings={"key_d": MappingAction(action_type=ActionType.KEYBOARD, target="key_right")},
        rollover_buttons={"key_play"},
    )

    await coordinator.apply_resolved_device_profile(manager, KEYBOARD, resolved)

    sent = [call.args[0] for call in manager.client.send_command.await_args_list]
    assert [command.command for command in sent] == [
        CommandType.GRAB_DEVICE,
        CommandType.SET_MAPPING,
    ]
    assert sorted(sent[0].data["evdev_paths"]) == ["/dev/input/event0", "/dev/input/event1"]
    assert "rollover_groups" not in sent[1].data


@pytest.mark.asyncio
async def test_empty_groups_are_sent_after_a_hotplug_refresh() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=Response(status="ok", data={}))
    await application.update_rollover_groups(manager, [_group()])

    invalidate_grabbed_state(manager)
    await application.update_rollover_groups(manager, [])

    assert manager.client.send_command.await_count == 2
    assert manager.client.send_command.await_args.args[0].data == {"groups": []}


@pytest.mark.asyncio
async def test_groups_are_sent_again_after_an_interrupted_send() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(
        side_effect=[
            Response(status="ok", data={}),
            asyncio.CancelledError(),
            Response(status="ok", data={}),
        ]
    )
    await application.update_rollover_groups(manager, [_group()])

    with pytest.raises(asyncio.CancelledError):
        await application.update_rollover_groups(manager, [])
    await application.update_rollover_groups(manager, [_group()])

    assert manager.client.send_command.await_count == 3


@pytest.mark.asyncio
async def test_empty_groups_are_sent_after_the_daemon_connection_drops() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=Response(status="ok", data={}))
    await application.update_rollover_groups(manager, [_group()])

    await manager._handle_keymasqd_disconnect()
    await application.update_rollover_groups(manager, [])

    assert manager.client.send_command.await_count == 2
    assert manager.client.send_command.await_args.args[0].data == {"groups": []}
