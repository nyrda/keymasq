from datetime import datetime
from pathlib import Path

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.profiles import (
    ComboConfig,
    ComboEvent,
    ComboStep,
    DeviceProfileLayer,
    ProfileConfig,
)
from keymasq.session.profile import references
from keymasq.session.profile.codec import ProfileCodec
from keymasq.session.profile.resolution import ProfileResolver
from keymasq.session.profile.types import ProfileInfo


def test_superkey_reference_rename_updates_mappings_and_combos_without_mutating_source() -> None:
    original = ProfileConfig(
        name="Desktop",
        device_layers={
            "keyboard": DeviceProfileLayer(
                hardware_id="keyboard",
                mappings={
                    "key_capslock": MappingAction(
                        action_type=ActionType.SUPERKEY,
                        superkey_name="Old",
                    )
                },
            )
        },
        combos=[
            ComboConfig(
                id="launcher",
                action=MappingAction(
                    action_type=ActionType.SUPERKEY,
                    superkey_name="Old",
                ),
            )
        ],
    )

    rewrite = references.rename_superkey(original, "Old", "New")

    assert rewrite.count == 2
    assert rewrite.config is not None
    mapping = rewrite.config.device_layers["keyboard"].mappings["key_capslock"]
    combo_action = rewrite.config.combos[0].action
    assert mapping.superkey_name == "New"
    assert combo_action is not None
    assert combo_action.superkey_name == "New"
    assert original.device_layers["keyboard"].mappings["key_capslock"].superkey_name == "Old"
    assert original.combos[0].action is not None
    assert original.combos[0].action.superkey_name == "Old"


def test_codec_reports_timestamp_repair_without_filesystem_state() -> None:
    decoded = ProfileCodec().decode(
        {
            "profile": {"name": "Portable", "is_permanent": True},
            "devices": {
                "mouse": {"mapping": {"btn_side": {"action": "keyboard", "target": "key_f13"}}}
            },
        },
        default_name="fallback",
        now=datetime(2026, 7, 10, 12, 0),
    )

    assert decoded.created_at_repair_reason == "missing created_at"
    assert decoded.config.created_at == datetime(2026, 7, 10, 12, 0)
    assert decoded.config.device_layers["mouse"].mappings["btn_side"].target == "key_f13"


def test_codec_round_trip_preserves_combo_scope() -> None:
    codec = ProfileCodec()
    original = ProfileConfig(
        name="Portable",
        created_at=datetime(2026, 7, 10, 12, 0),
        combos=[
            ComboConfig(
                id="portable-f13",
                name="Portable F13",
                steps=[
                    ComboStep(
                        events=[
                            ComboEvent(
                                evdev="key_f13",
                                hardware_id="keyboard",
                                source="kbd",
                            )
                        ],
                        timeout_ms=500,
                    )
                ],
                action=MappingAction(action_type=ActionType.SUPPRESS),
                match_across_devices=True,
            )
        ],
    )

    decoded = codec.decode(codec.encode(original), default_name="fallback")

    combo = decoded.config.combos[0]
    assert combo.match_across_devices is True
    assert combo.steps[0].timeout_ms == 500
    assert combo.steps[0].events[0].hardware_id == "keyboard"
    assert combo.steps[0].events[0].source == "kbd"


def test_resolver_applies_runtime_overlay_without_mutating_stored_combo() -> None:
    base = ProfileConfig(
        name="Base",
        enabled=True,
        is_permanent=True,
        created_at=datetime(2026, 7, 10, 12, 0),
        device_layers={
            "mouse": DeviceProfileLayer(
                hardware_id="mouse",
                mappings={
                    "btn_side": MappingAction(
                        action_type=ActionType.KEYBOARD,
                        target="key_1",
                    )
                },
            )
        },
    )
    overlay = ProfileConfig(
        name="Overlay",
        enabled=False,
        is_permanent=True,
        created_at=datetime(2026, 7, 10, 12, 1),
        device_layers={
            "mouse": DeviceProfileLayer(
                hardware_id="mouse",
                mappings={
                    "btn_side": MappingAction(
                        action_type=ActionType.KEYBOARD,
                        target="key_2",
                    )
                },
            )
        },
        combos=[
            ComboConfig(
                id="portable",
                name="Portable",
                steps=[
                    ComboStep(
                        events=[
                            ComboEvent(
                                evdev="key_f13",
                                hardware_id="keyboard",
                                source="kbd",
                            )
                        ]
                    )
                ],
                action=MappingAction(action_type=ActionType.SUPPRESS),
                match_across_devices=True,
            )
        ],
    )
    profiles = {
        profile.name: ProfileInfo(Path(f"{profile.name}.toml"), profile)
        for profile in (base, overlay)
    }

    resolved = ProfileResolver(profiles).resolve(runtime_profile_names=["Overlay"])

    assert [profile.name for profile in resolved.active_profiles] == ["Base", "Overlay"]
    assert resolved.devices["mouse"].mappings["btn_side"].target == "key_2"
    runtime_event = resolved.combos[0].steps[0].events[0]
    assert runtime_event.hardware_id == ""
    assert runtime_event.source is None
    stored_event = overlay.combos[0].steps[0].events[0]
    assert stored_event.hardware_id == "keyboard"
    assert stored_event.source == "kbd"
