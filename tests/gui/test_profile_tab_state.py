from datetime import datetime

from keymasq.common.model.core import ProfileState
from keymasq.common.model.profiles import ProfileConfig, WindowRule
from keymasq.gui.widgets.profile_tab.state import (
    ActiveProfiles,
    LifecycleMacroOptions,
    next_copy_name,
    profile_state,
    profile_state_icon,
)


def _config(**overrides: object) -> ProfileConfig:
    values: dict[str, object] = {
        "name": "work",
        "enabled": True,
        "is_permanent": False,
        "created_at": datetime.now(),
    }
    values.update(overrides)
    return ProfileConfig(**values)  # pyright: ignore[reportArgumentType]


def test_active_profiles_normalizes_summary_and_layer_order() -> None:
    state = ActiveProfiles.from_payload({"active_profiles": ["base", "work", "game", "overlay"]})

    assert state.names == ("base", "work", "game", "overlay")
    assert state.summary() == "base, work, game, +1"
    assert state.layer_tooltip() == "Layer order: base -> work -> game -> overlay"
    assert ActiveProfiles.from_payload({"active_profiles": "base"}).names == ()


def test_profile_state_and_icon_are_resolved_without_widgets() -> None:
    waiting = _config(window_rules=[WindowRule(field="title", pattern="Editor")])
    disabled = _config(enabled=False)

    assert profile_state(waiting, ()) is ProfileState.WAITING
    assert profile_state(waiting, ("work",)) is ProfileState.ACTIVE
    assert profile_state(disabled, ()) is ProfileState.INACTIVE
    assert profile_state_icon(disabled, (), unsupported_rules=False) == "🔴"
    assert profile_state_icon(waiting, (), unsupported_rules=True) == "❗"


def test_lifecycle_macro_options_parse_and_preserve_selected_missing_names() -> None:
    state = LifecycleMacroOptions.from_payload(
        {
            "macros": [
                {"name": "Zulu"},
                {"name": "alpha"},
                {"name": "alpha"},
                {"name": " "},
                "invalid",
            ]
        }
    )

    assert state.available == ("alpha", "Zulu")
    choices = state.choices("deleted macro", "alpha")
    assert choices == ("", "alpha", "Zulu", "deleted macro")
    assert state.index(choices, "Zulu") == 2
    assert state.index(choices, "missing") == 0
    assert state.selected_name(choices, 3) == "deleted macro"
    assert state.selected_name(choices, 99) is None


def test_next_copy_name_increments_existing_suffixes() -> None:
    existing = {"work", "work_1", "work_2", "other_8"}

    assert next_copy_name("work", existing) == "work_3"
    assert next_copy_name("other_8", existing) == "other_9"
