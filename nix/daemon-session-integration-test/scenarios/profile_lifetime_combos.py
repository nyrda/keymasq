import evdev
from support import TEMP_PROFILE_NAME, ScenarioContext

from .profile_lifetime_helpers import (
    assert_temporary_layer_persisted_disabled,
    expect_base_mapping,
    expect_temporary_mapping,
    reset_temporary_layer,
)


def run(ctx: ScenarioContext) -> None:
    reset_temporary_layer(ctx)

    ctx.source_key(evdev.ecodes.KEY_F9, 1)
    ctx.source_key(evdev.ecodes.KEY_F10, 1)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=True)
    assert_temporary_layer_persisted_disabled(ctx)
    expect_temporary_mapping(ctx)
    ctx.source_key(evdev.ecodes.KEY_F10, 0)
    ctx.source_key(evdev.ecodes.KEY_F9, 0)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=False)
    expect_base_mapping(ctx)

    ctx.source_key(evdev.ecodes.KEY_F11, 1)
    ctx.source_key(evdev.ecodes.KEY_F12, 1)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=True)
    assert_temporary_layer_persisted_disabled(ctx)
    expect_temporary_mapping(ctx)
    ctx.source_key(evdev.ecodes.KEY_F12, 0)
    ctx.source_key(evdev.ecodes.KEY_F11, 0)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=False)
    expect_base_mapping(ctx)
