import time

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

    ctx.source_key(evdev.ecodes.KEY_F1, 1)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=True)
    assert_temporary_layer_persisted_disabled(ctx)
    expect_temporary_mapping(ctx)
    ctx.source_key(evdev.ecodes.KEY_F1, 0)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=False)
    expect_base_mapping(ctx)

    ctx.tap_source(evdev.ecodes.KEY_F2)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=True)
    assert_temporary_layer_persisted_disabled(ctx)
    expect_temporary_mapping(ctx)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=False)
    expect_base_mapping(ctx)

    reset_temporary_layer(ctx)
    ctx.tap_source(evdev.ecodes.KEY_F2)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=True)
    ctx.tap_source(evdev.ecodes.KEY_Z)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=False)
    expect_base_mapping(ctx)

    ctx.tap_source(evdev.ecodes.KEY_F3)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=True)
    expect_temporary_mapping(ctx)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=True)
    expect_temporary_mapping(ctx)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=False)
    expect_base_mapping(ctx)

    ctx.tap_source(evdev.ecodes.KEY_F4)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=True)
    assert_temporary_layer_persisted_disabled(ctx)
    time.sleep(1.4)
    ctx.wait_for_active_profile(TEMP_PROFILE_NAME, enabled=False)
    expect_base_mapping(ctx)
