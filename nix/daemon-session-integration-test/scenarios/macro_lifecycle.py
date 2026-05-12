import evdev
from support import ScenarioContext

MACRO_NAME = "integration-lifecycle-macro"
RENAMED_MACRO_NAME = "integration-lifecycle-renamed"


def key_events(code: int) -> list[dict[str, object]]:
    return [
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": code,
            "value": 1,
            "t_us": 0,
        },
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": code,
            "value": 0,
            "t_us": 10000,
        },
    ]


def run(ctx: ScenarioContext) -> None:
    ctx.request({"command": "delete_macro", "name": MACRO_NAME}, ok=False)
    ctx.request({"command": "delete_macro", "name": RENAMED_MACRO_NAME}, ok=False)

    ctx.request(
        {
            "command": "create_macro",
            "macro": {"name": MACRO_NAME, "events": key_events(evdev.ecodes.KEY_1)},
        }
    )
    ctx.request({"command": "play_macro", "name": MACRO_NAME})
    ctx.expect_keys([(evdev.ecodes.KEY_1, 1), (evdev.ecodes.KEY_1, 0)])

    ctx.request(
        {
            "command": "update_macro",
            "name": MACRO_NAME,
            "macro": {"name": MACRO_NAME, "events": key_events(evdev.ecodes.KEY_2)},
        }
    )
    ctx.request({"command": "play_macro", "name": MACRO_NAME})
    ctx.expect_keys([(evdev.ecodes.KEY_2, 1), (evdev.ecodes.KEY_2, 0)])

    ctx.request({"command": "rename_macro", "old": MACRO_NAME, "new": RENAMED_MACRO_NAME})
    ctx.request({"command": "play_macro", "name": RENAMED_MACRO_NAME})
    ctx.expect_keys([(evdev.ecodes.KEY_2, 1), (evdev.ecodes.KEY_2, 0)])

    ctx.request(
        {
            "command": "play_macro_payload",
            "macro_events": key_events(evdev.ecodes.KEY_3),
            "loop_mode": "count",
            "loop_count": 2,
        }
    )
    ctx.expect_keys(
        [
            (evdev.ecodes.KEY_3, 1),
            (evdev.ecodes.KEY_3, 0),
            (evdev.ecodes.KEY_3, 1),
            (evdev.ecodes.KEY_3, 0),
        ]
    )

    ctx.request({"command": "delete_macro", "name": RENAMED_MACRO_NAME})
    result = ctx.request({"command": "play_macro", "name": RENAMED_MACRO_NAME}, ok=False)
    if result.get("status") == "ok":
        raise AssertionError("deleted macro still played successfully")
