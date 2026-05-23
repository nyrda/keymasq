from support import ScenarioCase

from .analog_mouse_area import run as analog_mouse_area
from .analog_mouse_velocity import run as analog_mouse_velocity
from .analog_multi_control import run as analog_multi_control
from .analog_priority_override import run as analog_priority_override
from .analog_profile_reset import run as analog_profile_reset
from .analog_restart_recovery import run as analog_restart_recovery
from .analog_signed_axis_mouse import run as analog_signed_axis_mouse
from .analog_signed_axis_threshold import run as analog_signed_axis_threshold
from .analog_stick_gamepad import run as analog_stick_gamepad
from .analog_threshold_hysteresis import run as analog_threshold_hysteresis
from .analog_trigger_deadzone import run as analog_trigger_deadzone
from .cancel_macro_playback import run as cancel_macro_playback
from .combo_chord import run as combo_chord
from .combo_negative_timeout import run as combo_negative_timeout
from .combo_prefix_overlap import run as combo_prefix_overlap
from .combo_superkey import run as combo_superkey
from .combo_superkey_recall_restore import run as combo_superkey_recall_restore
from .emergency_reset import run as emergency_reset
from .gamepad_axis_output import run as gamepad_axis_output
from .gamepad_output import run as gamepad_output
from .held_output_profile_change import run as held_output_profile_change
from .hotplug_replug import run as hotplug_replug
from .macro_lifecycle import run as macro_lifecycle
from .macro_playback import run as macro_playback
from .mouse_output import run as mouse_output
from .multi_source_combo import run as multi_source_combo
from .multi_step_combo import run as multi_step_combo
from .passthrough_fallback import run as passthrough_override
from .profile_lifetime_combos import run as profile_lifetime_combos
from .profile_lifetime_direct import run as profile_lifetime_direct
from .profile_lifetime_superkeys import run as profile_lifetime_superkeys
from .profile_lifetime_toggle import run as profile_lifetime_toggle
from .profile_toggle import run as profile_toggle
from .rapidfire_keyboard import run as rapidfire_keyboard
from .recording_capture import run as recording_capture
from .restart_recovery import run as restart_recovery
from .simple_remap import run as simple_remap
from .superkey_overload_multi_action import run as superkey_overload_multi_action
from .superkey_tap import run as superkey_tap
from .suppress import run as suppress
from .tap_enabled import run as tap_enabled

SCENARIOS = [
    ScenarioCase("simple 1->1 remap", simple_remap),
    ScenarioCase("suppress", suppress),
    ScenarioCase("tap-enabled keyboard action", tap_enabled),
    ScenarioCase("rapidfire keyboard action", rapidfire_keyboard),
    ScenarioCase("macro playback", macro_playback),
    ScenarioCase("cancel macro playback", cancel_macro_playback),
    ScenarioCase("macro lifecycle", macro_lifecycle),
    ScenarioCase("superkey tap", superkey_tap),
    ScenarioCase("superkey overload multi-action press/release", superkey_overload_multi_action),
    ScenarioCase("combo chord", combo_chord),
    ScenarioCase("combo bound to superkey", combo_superkey),
    ScenarioCase("combo superkey recall and restore", combo_superkey_recall_restore),
    ScenarioCase("multi-step combo", multi_step_combo),
    ScenarioCase("combo negative and timeout", combo_negative_timeout),
    ScenarioCase("prefix and overlapping combos", combo_prefix_overlap),
    ScenarioCase("multi-source combo", multi_source_combo),
    ScenarioCase("profile toggle", profile_toggle),
    ScenarioCase("profile lifetime direct actions", profile_lifetime_direct),
    ScenarioCase("profile lifetime toggle actions", profile_lifetime_toggle),
    ScenarioCase("profile lifetime superkeys", profile_lifetime_superkeys),
    ScenarioCase("profile lifetime combos", profile_lifetime_combos),
    ScenarioCase("passthrough overrides lower-profile mapping", passthrough_override),
    ScenarioCase("held output profile change", held_output_profile_change),
    ScenarioCase("mouse output", mouse_output),
    ScenarioCase("gamepad output", gamepad_output),
    ScenarioCase("gamepad axis output", gamepad_axis_output),
    ScenarioCase("analog stick to gamepad stick", analog_stick_gamepad),
    ScenarioCase("analog trigger deadzone", analog_trigger_deadzone),
    ScenarioCase("analog threshold hysteresis", analog_threshold_hysteresis),
    ScenarioCase("analog signed axis threshold ranges", analog_signed_axis_threshold),
    ScenarioCase("analog state reset on profile change", analog_profile_reset),
    ScenarioCase("multiple analog controls on one source", analog_multi_control),
    ScenarioCase("analog stick to mouse area", analog_mouse_area),
    ScenarioCase("analog stick to mouse velocity", analog_mouse_velocity),
    ScenarioCase("analog signed axis mouse horizontal", analog_signed_axis_mouse),
    ScenarioCase("analog priority override", analog_priority_override),
    ScenarioCase("analog restart recovery", analog_restart_recovery),
    ScenarioCase("emergency reset", emergency_reset),
    ScenarioCase("recording and capture", recording_capture),
    ScenarioCase("restart recovery", restart_recovery),
    ScenarioCase("hotplug replug", hotplug_replug),
]
