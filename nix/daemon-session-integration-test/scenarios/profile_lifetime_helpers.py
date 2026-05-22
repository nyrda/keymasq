import evdev
from support import TEMP_PROFILE_NAME, ScenarioContext


def reset_temporary_layer(ctx: ScenarioContext) -> None:
    ctx.set_profile_enabled(TEMP_PROFILE_NAME, enabled=False)
    assert_temporary_layer_persisted_disabled(ctx)
    ctx.drain_outputs()


def assert_temporary_layer_persisted_disabled(ctx: ScenarioContext) -> None:
    result = ctx.request({"command": "list_profiles"})
    profiles = result.get("profiles", [])
    for profile in profiles:
        if isinstance(profile, dict) and profile.get("name") == TEMP_PROFILE_NAME:
            if profile.get("enabled") is not False:
                raise AssertionError(f"temporary profile was persisted enabled: {profile}")
            return
    raise AssertionError(f"temporary profile not listed: {profiles}")


def expect_base_mapping(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_A)
    ctx.expect_keys([(evdev.ecodes.KEY_Q, 1), (evdev.ecodes.KEY_Q, 0)])


def expect_temporary_mapping(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_A)
    ctx.expect_keys([(evdev.ecodes.KEY_H, 1), (evdev.ecodes.KEY_H, 0)])
