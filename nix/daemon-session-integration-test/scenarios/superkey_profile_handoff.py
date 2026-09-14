import json
import shlex
import time
from pathlib import Path

import evdev
from support import HARDWARE_ID, PROFILE_NAME, SECOND_PROFILE_NAME, ScenarioContext

PROFILE = "Integration Superkey Handoff"


def _expect_lines(path: Path, expected: list[str]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().splitlines() == expected:
            return
        time.sleep(0.05)
    actual = path.read_text() if path.exists() else "<missing>"
    raise AssertionError(f"Expected command log {expected}, got {actual}")


def run(ctx: ScenarioContext) -> None:
    marker = ctx.config_dir / "superkey-handoff.log"
    overload = ctx.config_dir / "superkeys" / "integration-handoff-overload.toml"
    pattern = ctx.config_dir / "superkeys" / "integration-handoff-pattern.toml"
    profile = ctx.config_dir / "profiles" / "integration-superkey-handoff.toml"
    start = json.dumps(f"printf 'start\\n' >> {shlex.quote(str(marker))}")
    stop = json.dumps(f"printf 'stop\\n' >> {shlex.quote(str(marker))}")
    overload.write_text(
        'name = "integration-handoff-overload"\nmode = "overload"\n[actions]\n'
        f'overload_down = [{{ action = "exec", cmd = {start} }}]\n'
        f'overload_up = [{{ action = "exec", cmd = {stop} }}]\n'
    )
    pattern.write_text(
        'name = "integration-handoff-pattern"\nmode = "pattern"\n'
        "[timing]\nhold_threshold_ms = 30\n[actions]\n"
        'hold = [{ action = "keyboard", target = "key_leftctrl" }]\n'
    )
    profile.write_text(
        f'[profile]\nname = "{PROFILE}"\nenabled = false\npriority = 900\n'
        'is_permanent = true\nnotify_on_activation = false\n'
        'created_at = "2026-09-14T00:00:00"\n'
        f'[devices."{HARDWARE_ID}".mapping.key_f24]\n'
        'action = "superkey"\nsuperkey_name = "integration-handoff-overload"\n'
        f'[devices."{HARDWARE_ID}".mapping.key_f23]\n'
        'action = "superkey"\nsuperkey_name = "integration-handoff-pattern"\n'
    )
    try:
        ctx.request({"command": "reload"})
        ctx.set_profile_enabled(PROFILE, enabled=True)
        marker.unlink(missing_ok=True)
        ctx.source_key(evdev.ecodes.KEY_F24, 1)
        _expect_lines(marker, ["start"])
        ctx.set_profile_enabled(SECOND_PROFILE_NAME, enabled=True)
        ctx.set_profile_enabled(SECOND_PROFILE_NAME, enabled=False)
        time.sleep(1.2)
        _expect_lines(marker, ["start"])
        ctx.source_key(evdev.ecodes.KEY_F24, 0)
        _expect_lines(marker, ["start", "stop"])

        ctx.source_key(evdev.ecodes.KEY_F23, 1)
        ctx.expect_keys([(evdev.ecodes.KEY_LEFTCTRL, 1)])
        ctx.set_profile_enabled(PROFILE, enabled=False)
        ctx.expect_no_keyboard_events()
        ctx.source_key(evdev.ecodes.KEY_F23, 0)
        ctx.expect_keys([(evdev.ecodes.KEY_LEFTCTRL, 0)])

        ctx.set_profile_enabled(PROFILE, enabled=True)
        ctx.set_profile_enabled(PROFILE_NAME, enabled=False)
        marker.unlink(missing_ok=True)
        ctx.source_key(evdev.ecodes.KEY_F24, 1)
        _expect_lines(marker, ["start"])
        ctx.set_profile_enabled(PROFILE, enabled=False)
        time.sleep(1.2)
        _expect_lines(marker, ["start"])
        ctx.source_key(evdev.ecodes.KEY_F24, 0)
        _expect_lines(marker, ["start", "stop"])
    finally:
        ctx.source_key(evdev.ecodes.KEY_F24, 0)
        ctx.source_key(evdev.ecodes.KEY_F23, 0)
        ctx.set_profile_enabled(PROFILE_NAME, enabled=True)
        ctx.set_profile_enabled(SECOND_PROFILE_NAME, enabled=False)
        ctx.request({"command": "disable_profile", "profile_name": PROFILE}, ok=False)
        profile.unlink(missing_ok=True)
        overload.unlink(missing_ok=True)
        pattern.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        ctx.request({"command": "reload"})
        ctx.reopen_outputs()
