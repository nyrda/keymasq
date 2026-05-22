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

    ctx.tap_source(evdev.ecodes.KEY_F5)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=True)
    assert_temporary_layer_persisted_disabled(ctx)
    ctx.tap_source(evdev.ecodes.KEY_F5)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=False)
    expect_base_mapping(ctx)

    ctx.tap_source(evdev.ecodes.KEY_F5)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=True)
    expect_temporary_mapping(ctx)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=False)
    expect_base_mapping(ctx)

    ctx.tap_source(evdev.ecodes.KEY_F6)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=True)
    expect_temporary_mapping(ctx)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=True)
    ctx.tap_source(evdev.ecodes.KEY_F6)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=False)
    expect_base_mapping(ctx)
