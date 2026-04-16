from datetime import datetime, timedelta

from keymasq.common.models import (
    ActionType,
    DeviceProfileLayer,
    MappingAction,
    ProfileConfig,
    WindowRule,
)
from keymasq.session.profiles import ProfileManager


def _profile(
    name: str,
    *,
    permanent: bool,
    priority: int,
    created_at: datetime,
    target: str | None,
    rules: list[WindowRule] | None = None,
) -> ProfileConfig:
    mappings = {}
    if target is not None:
        mappings["btn_back"] = MappingAction(action_type=ActionType.KEYBOARD, target=target)
    else:
        mappings["btn_back"] = MappingAction(action_type=ActionType.PASSTHROUGH)
    return ProfileConfig(
        name=name,
        enabled=True,
        is_permanent=permanent,
        priority=priority,
        created_at=created_at,
        window_rules=rules or [],
        device_layers={
            "1234:5678": DeviceProfileLayer(hardware_id="1234:5678", mappings=mappings)
        },
    )


class TestProfileMerging:
    def test_higher_priority_wins(self, temp_config_dir):
        manager = ProfileManager()
        manager.save_profile(
            _profile(
                "Low",
                permanent=True,
                priority=1,
                created_at=datetime.now(),
                target="key_1",
            )
        )
        manager.save_profile(
            _profile(
                "High",
                permanent=True,
                priority=10,
                created_at=datetime.now(),
                target="key_2",
            )
        )

        resolved = manager.resolve_active_profiles(hardware_ids=["1234:5678"])

        assert resolved.devices["1234:5678"].mappings["btn_back"].target == "key_2"

    def test_newer_profile_wins_same_priority(self, temp_config_dir):
        manager = ProfileManager()
        now = datetime.now()
        manager.save_profile(
            _profile("Older", permanent=True, priority=5, created_at=now, target="key_1")
        )
        manager.save_profile(
            _profile(
                "Newer",
                permanent=True,
                priority=5,
                created_at=now + timedelta(seconds=1),
                target="key_2",
            )
        )

        resolved = manager.resolve_active_profiles(hardware_ids=["1234:5678"])

        assert resolved.devices["1234:5678"].mappings["btn_back"].target == "key_2"

    def test_conditional_overlays_permanent(self, temp_config_dir):
        manager = ProfileManager()
        now = datetime.now()
        manager.save_profile(
            _profile("Base", permanent=True, priority=10, created_at=now, target="key_1")
        )
        manager.save_profile(
            _profile(
                "Game",
                permanent=False,
                priority=1,
                created_at=now,
                target="key_2",
                rules=[WindowRule(field="class", pattern="steam")],
            )
        )

        resolved = manager.resolve_active_profiles(
            window_info={"class": "steam", "title": "Steam", "tags": []},
            hardware_ids=["1234:5678"],
        )

        assert resolved.devices["1234:5678"].mappings["btn_back"].target == "key_2"

    def test_explicit_passthrough_masks_lower_layer(self, temp_config_dir):
        manager = ProfileManager()
        now = datetime.now()
        manager.save_profile(
            _profile("Base", permanent=True, priority=1, created_at=now, target="key_1")
        )
        manager.save_profile(
            _profile(
                "Mask",
                permanent=False,
                priority=1,
                created_at=now,
                target=None,
                rules=[WindowRule(field="class", pattern="steam")],
            )
        )

        resolved = manager.resolve_active_profiles(
            window_info={"class": "steam", "title": "Steam", "tags": []},
            hardware_ids=["1234:5678"],
        )

        assert "btn_back" not in resolved.devices["1234:5678"].mappings
