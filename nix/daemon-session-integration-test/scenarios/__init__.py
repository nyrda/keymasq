from support import ScenarioCase

from .cancel_macro_playback import run as cancel_macro_playback
from .combo_chord import run as combo_chord
from .combo_negative_timeout import run as combo_negative_timeout
from .combo_prefix_overlap import run as combo_prefix_overlap
from .combo_superkey import run as combo_superkey
from .combo_superkey_recall_restore import run as combo_superkey_recall_restore
from .emergency_reset import run as emergency_reset
from .gamepad_output import run as gamepad_output
from .held_output_profile_change import run as held_output_profile_change
from .hotplug_replug import run as hotplug_replug
from .macro_lifecycle import run as macro_lifecycle
from .macro_playback import run as macro_playback
from .mouse_output import run as mouse_output
from .multi_source_combo import run as multi_source_combo
from .multi_step_combo import run as multi_step_combo
from .passthrough_fallback import run as passthrough_override
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
    ScenarioCase("passthrough overrides lower-profile mapping", passthrough_override),
    ScenarioCase("held output profile change", held_output_profile_change),
    ScenarioCase("mouse output", mouse_output),
    ScenarioCase("gamepad output", gamepad_output),
    ScenarioCase("emergency reset", emergency_reset),
    ScenarioCase("recording and capture", recording_capture),
    ScenarioCase("restart recovery", restart_recovery),
    ScenarioCase("hotplug replug", hotplug_replug),
]
