"""Enums shared by the domain model modules."""

from enum import Enum


class ActionType(Enum):
    PASSTHROUGH = "passthrough"
    KEYBOARD = "keyboard"
    MOUSE = "mouse"
    GAMEPAD = "gamepad"
    GAMEPAD_AXIS = "gamepad_axis"
    ANALOG_CONTROL = "analog_control"
    EXEC = "exec"
    COMPOSITOR_DISPATCH = "compositor_dispatch"
    SUPPRESS = "suppress"
    SUPERKEY = "superkey"
    START_MACRO_RECORDING = "start_macro_recording"
    STOP_MACRO_RECORDING = "stop_macro_recording"
    PLAY_MACRO_SLOT = "play_macro_slot"
    CANCEL_MACRO_PLAYBACK = "cancel_macro_playback"
    EMERGENCY_RESET = "emergency_reset"
    MACRO = "macro"
    MOUSE_MOVE_REL = "mouse_move_rel"
    MOUSE_MOVE_ABS = "mouse_move_abs"
    MOUSE_MOVE_NATURAL_ABS = "mouse_move_natural_abs"
    PROFILE_ENABLE = "profile_enable"
    PROFILE_DISABLE = "profile_disable"
    PROFILE_TOGGLE = "profile_toggle"
    MPRIS = "mpris"
    REPEAT = "repeat"


class SuperkeyMode(Enum):
    PATTERN = "pattern"
    OVERLOAD = "overload"


class DeviceType(Enum):
    MOUSE = "mouse"
    KEYBOARD = "keyboard"
    GAMEPAD = "gamepad"
    OTHER = "other"


class ProfileState(Enum):
    INACTIVE = "inactive"
    WAITING = "waiting"
    ACTIVE = "active"
    STANDBY = "standby"


class WindowFieldType(Enum):
    CLASS = "class"
    TITLE = "title"
    INITIAL_CLASS = "initial_class"
    INITIAL_TITLE = "initial_title"
    TAG = "tag"
