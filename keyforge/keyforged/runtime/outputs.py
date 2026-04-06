from collections.abc import Sequence
from typing import Any, cast


def create_global_uinputs(
    manager: Any,
    *,
    evdev_mod: Any,
    log: Any,
    uinput_writer: Any,
) -> None:
    if manager._device_count == 0:
        log.info("Creating global output uinput devices")

        keyboard_caps = {
            evdev_mod.ecodes.EV_KEY: [
                evdev_mod.ecodes.KEY_ESC,
                evdev_mod.ecodes.KEY_1,
                evdev_mod.ecodes.KEY_2,
                evdev_mod.ecodes.KEY_3,
                evdev_mod.ecodes.KEY_4,
                evdev_mod.ecodes.KEY_5,
                evdev_mod.ecodes.KEY_6,
                evdev_mod.ecodes.KEY_7,
                evdev_mod.ecodes.KEY_8,
                evdev_mod.ecodes.KEY_9,
                evdev_mod.ecodes.KEY_0,
                evdev_mod.ecodes.KEY_MINUS,
                evdev_mod.ecodes.KEY_EQUAL,
                evdev_mod.ecodes.KEY_BACKSPACE,
                evdev_mod.ecodes.KEY_TAB,
                evdev_mod.ecodes.KEY_Q,
                evdev_mod.ecodes.KEY_W,
                evdev_mod.ecodes.KEY_E,
                evdev_mod.ecodes.KEY_R,
                evdev_mod.ecodes.KEY_T,
                evdev_mod.ecodes.KEY_Y,
                evdev_mod.ecodes.KEY_U,
                evdev_mod.ecodes.KEY_I,
                evdev_mod.ecodes.KEY_O,
                evdev_mod.ecodes.KEY_P,
                evdev_mod.ecodes.KEY_LEFTBRACE,
                evdev_mod.ecodes.KEY_RIGHTBRACE,
                evdev_mod.ecodes.KEY_ENTER,
                evdev_mod.ecodes.KEY_LEFTCTRL,
                evdev_mod.ecodes.KEY_A,
                evdev_mod.ecodes.KEY_S,
                evdev_mod.ecodes.KEY_D,
                evdev_mod.ecodes.KEY_F,
                evdev_mod.ecodes.KEY_G,
                evdev_mod.ecodes.KEY_H,
                evdev_mod.ecodes.KEY_J,
                evdev_mod.ecodes.KEY_K,
                evdev_mod.ecodes.KEY_L,
                evdev_mod.ecodes.KEY_SEMICOLON,
                evdev_mod.ecodes.KEY_APOSTROPHE,
                evdev_mod.ecodes.KEY_GRAVE,
                evdev_mod.ecodes.KEY_LEFTSHIFT,
                evdev_mod.ecodes.KEY_BACKSLASH,
                evdev_mod.ecodes.KEY_102ND,
                evdev_mod.ecodes.KEY_Z,
                evdev_mod.ecodes.KEY_X,
                evdev_mod.ecodes.KEY_C,
                evdev_mod.ecodes.KEY_V,
                evdev_mod.ecodes.KEY_B,
                evdev_mod.ecodes.KEY_N,
                evdev_mod.ecodes.KEY_M,
                evdev_mod.ecodes.KEY_COMMA,
                evdev_mod.ecodes.KEY_DOT,
                evdev_mod.ecodes.KEY_SLASH,
                evdev_mod.ecodes.KEY_RIGHTSHIFT,
                evdev_mod.ecodes.KEY_LEFTALT,
                evdev_mod.ecodes.KEY_LEFTMETA,
                evdev_mod.ecodes.KEY_SPACE,
                evdev_mod.ecodes.KEY_CAPSLOCK,
                evdev_mod.ecodes.KEY_F1,
                evdev_mod.ecodes.KEY_F2,
                evdev_mod.ecodes.KEY_F3,
                evdev_mod.ecodes.KEY_F4,
                evdev_mod.ecodes.KEY_F5,
                evdev_mod.ecodes.KEY_F6,
                evdev_mod.ecodes.KEY_F7,
                evdev_mod.ecodes.KEY_F8,
                evdev_mod.ecodes.KEY_F9,
                evdev_mod.ecodes.KEY_F10,
                evdev_mod.ecodes.KEY_F11,
                evdev_mod.ecodes.KEY_F12,
                evdev_mod.ecodes.KEY_RIGHTCTRL,
                evdev_mod.ecodes.KEY_RIGHTALT,
                evdev_mod.ecodes.KEY_RIGHTMETA,
                evdev_mod.ecodes.KEY_MENU,
                evdev_mod.ecodes.KEY_SYSRQ,
                evdev_mod.ecodes.KEY_SCROLLLOCK,
                evdev_mod.ecodes.KEY_PAUSE,
                evdev_mod.ecodes.KEY_HOME,
                evdev_mod.ecodes.KEY_UP,
                evdev_mod.ecodes.KEY_PAGEUP,
                evdev_mod.ecodes.KEY_LEFT,
                evdev_mod.ecodes.KEY_RIGHT,
                evdev_mod.ecodes.KEY_END,
                evdev_mod.ecodes.KEY_DOWN,
                evdev_mod.ecodes.KEY_PAGEDOWN,
                evdev_mod.ecodes.KEY_INSERT,
                evdev_mod.ecodes.KEY_DELETE,
                evdev_mod.ecodes.KEY_MUTE,
                evdev_mod.ecodes.KEY_VOLUMEDOWN,
                evdev_mod.ecodes.KEY_VOLUMEUP,
                evdev_mod.ecodes.KEY_NUMLOCK,
                evdev_mod.ecodes.KEY_KPSLASH,
                evdev_mod.ecodes.KEY_KPASTERISK,
                evdev_mod.ecodes.KEY_KPMINUS,
                evdev_mod.ecodes.KEY_KP7,
                evdev_mod.ecodes.KEY_KP8,
                evdev_mod.ecodes.KEY_KP9,
                evdev_mod.ecodes.KEY_KPPLUS,
                evdev_mod.ecodes.KEY_KP4,
                evdev_mod.ecodes.KEY_KP5,
                evdev_mod.ecodes.KEY_KP6,
                evdev_mod.ecodes.KEY_KP1,
                evdev_mod.ecodes.KEY_KP2,
                evdev_mod.ecodes.KEY_KP3,
                evdev_mod.ecodes.KEY_KPENTER,
                evdev_mod.ecodes.KEY_KP0,
                evdev_mod.ecodes.KEY_KPDOT,
            ],
            evdev_mod.ecodes.EV_SYN: [],
        }
        manager._keyboard_uinput = evdev_mod.UInput(
            events=cast(dict[int, Sequence[int]], keyboard_caps),
            name="keyforge-keyboard",
        )

        mouse_caps = {
            evdev_mod.ecodes.EV_KEY: [
                evdev_mod.ecodes.BTN_LEFT,
                evdev_mod.ecodes.BTN_RIGHT,
                evdev_mod.ecodes.BTN_MIDDLE,
                evdev_mod.ecodes.BTN_SIDE,
                evdev_mod.ecodes.BTN_EXTRA,
                evdev_mod.ecodes.BTN_FORWARD,
                evdev_mod.ecodes.BTN_BACK,
            ],
            evdev_mod.ecodes.EV_REL: [
                evdev_mod.ecodes.REL_X,
                evdev_mod.ecodes.REL_Y,
                evdev_mod.ecodes.REL_WHEEL,
                evdev_mod.ecodes.REL_HWHEEL,
            ],
            evdev_mod.ecodes.EV_SYN: [],
        }
        manager._mouse_uinput = evdev_mod.UInput(
            events=cast(dict[int, Sequence[int]], mouse_caps),
            name="keyforge-mouse",
        )

        gamepad_caps = {
            evdev_mod.ecodes.EV_KEY: [
                evdev_mod.ecodes.BTN_SOUTH,
                evdev_mod.ecodes.BTN_EAST,
                evdev_mod.ecodes.BTN_NORTH,
                evdev_mod.ecodes.BTN_WEST,
                evdev_mod.ecodes.BTN_TL,
                evdev_mod.ecodes.BTN_TR,
                evdev_mod.ecodes.BTN_TL2,
                evdev_mod.ecodes.BTN_TR2,
                evdev_mod.ecodes.BTN_SELECT,
                evdev_mod.ecodes.BTN_START,
                evdev_mod.ecodes.BTN_MODE,
                evdev_mod.ecodes.BTN_THUMBL,
                evdev_mod.ecodes.BTN_THUMBR,
                evdev_mod.ecodes.BTN_DPAD_UP,
                evdev_mod.ecodes.BTN_DPAD_DOWN,
                evdev_mod.ecodes.BTN_DPAD_LEFT,
                evdev_mod.ecodes.BTN_DPAD_RIGHT,
            ],
            evdev_mod.ecodes.EV_ABS: [
                (evdev_mod.ecodes.ABS_X, evdev_mod.AbsInfo(0, -32768, 32767, 16, 128, 0)),
                (evdev_mod.ecodes.ABS_Y, evdev_mod.AbsInfo(0, -32768, 32767, 16, 128, 0)),
                (evdev_mod.ecodes.ABS_RX, evdev_mod.AbsInfo(0, -32768, 32767, 16, 128, 0)),
                (evdev_mod.ecodes.ABS_RY, evdev_mod.AbsInfo(0, -32768, 32767, 16, 128, 0)),
                (evdev_mod.ecodes.ABS_Z, evdev_mod.AbsInfo(0, 0, 255, 0, 0, 0)),
                (evdev_mod.ecodes.ABS_RZ, evdev_mod.AbsInfo(0, 0, 255, 0, 0, 0)),
                (evdev_mod.ecodes.ABS_HAT0X, evdev_mod.AbsInfo(0, -1, 1, 0, 0, 0)),
                (evdev_mod.ecodes.ABS_HAT0Y, evdev_mod.AbsInfo(0, -1, 1, 0, 0, 0)),
            ],
            evdev_mod.ecodes.EV_SYN: [],
        }
        manager._gamepad_uinput = evdev_mod.UInput(
            events=cast(dict[int, Sequence[int]], gamepad_caps),
            name="Microsoft X-Box 360 pad",
            vendor=0x045E,
            product=0x028E,
            version=0x0110,
            bustype=0x0003,
        )

        gamepad_uinput = uinput_writer(manager._gamepad_uinput)
        if gamepad_uinput is not None:
            gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, evdev_mod.ecodes.ABS_X, 0)
            gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, evdev_mod.ecodes.ABS_Y, 0)
            gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, evdev_mod.ecodes.ABS_RX, 0)
            gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, evdev_mod.ecodes.ABS_RY, 0)
            gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, evdev_mod.ecodes.ABS_Z, 0)
            gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, evdev_mod.ecodes.ABS_RZ, 0)
            gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, evdev_mod.ecodes.ABS_HAT0X, 0)
            gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, evdev_mod.ecodes.ABS_HAT0Y, 0)
            gamepad_uinput.syn()

    manager._device_count += 1


def destroy_global_uinputs(manager: Any, *, log: Any) -> None:
    manager._device_count = max(0, manager._device_count - 1)

    if manager._device_count == 0:
        log.info("Destroying global output uinput devices")

        for uinput_dev in [
            manager._keyboard_uinput,
            manager._mouse_uinput,
            manager._gamepad_uinput,
        ]:
            if uinput_dev:
                try:
                    uinput_dev.close()
                except Exception as exc:
                    log.warning("Failed to close global uinput device: %s", exc)

        manager._keyboard_uinput = None
        manager._mouse_uinput = None
        manager._gamepad_uinput = None
