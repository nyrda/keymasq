import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.subtest("superkey release within tap timeout emits tap", lambda: _quick_tap(ctx))
    ctx.subtest("superkey release after tap timeout emits nothing", lambda: _slow_tap(ctx))


def _quick_tap(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_B, pause_s=0.05)
    ctx.expect_keys([(evdev.ecodes.KEY_W, 1), (evdev.ecodes.KEY_W, 0)])


def _slow_tap(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_B, pause_s=0.15)
    ctx.expect_no_keyboard_events()
