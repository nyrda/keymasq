from dataclasses import dataclass, field

import pytest

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType, SuperkeyMode
from keymasq.common.model.superkeys import SuperkeyAction, SuperkeyConfig
from keymasq.gui.widgets.managed_editor.state import EditorSelection, EditorState
from keymasq.gui.widgets.superkey_editor.draft import SuperkeyDraft
from keymasq.gui.widgets.superkey_editor.persistence import SuperkeyPersistence


def test_editor_state_consumes_guarded_transition() -> None:
    state = EditorState()
    state.activate(EditorSelection.saved_item("Existing"))
    state.mark_dirty()

    assert state.new_draft_restart_needs_confirmation(pristine_draft=True) is True
    state.queue_transition(EditorSelection.new_item(), restart_new_item=True)

    pending = state.take_pending_transition()
    assert pending is not None
    assert pending.selection == EditorSelection.new_item()
    assert pending.restart_new_item is True
    assert state.pending_transition is None


def test_superkey_draft_serializes_only_the_selected_mode() -> None:
    pattern_action = SuperkeyAction(action_type=ActionType.KEYBOARD, target="key_a")
    overload_action = MappingAction(action_type=ActionType.KEYBOARD, target="key_leftctrl")
    pattern = SuperkeyDraft(
        name=" Pattern ",
        description=" Description ",
        mode=SuperkeyMode.PATTERN,
        tap_actions=(pattern_action,),
        overload_actions=(overload_action,),
    ).to_config()

    assert pattern.name == "Pattern"
    assert pattern.description == "Description"
    assert pattern.tap_actions == [pattern_action]
    assert pattern.overload_actions == []

    overload = SuperkeyDraft(
        name="Overload",
        description="",
        mode=SuperkeyMode.OVERLOAD,
        tap_actions=(pattern_action,),
        overload_actions=(overload_action,),
    ).to_config()
    assert overload.tap_actions == []
    assert overload.overload_actions == [overload_action]
    assert SuperkeyDraft.new().is_pristine_new_draft() is True


@dataclass
class _Store:
    values: dict[str, SuperkeyConfig] = field(default_factory=dict)
    saved: list[tuple[str, str | None]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def save_superkey(
        self,
        config: SuperkeyConfig,
        *,
        replacing_name: str | None = None,
    ) -> None:
        if replacing_name != config.name and config.name in self.values:
            raise ValueError(f"Superkey '{config.name}' already exists")
        if replacing_name is not None and replacing_name != config.name:
            self.values.pop(replacing_name, None)
        self.values[config.name] = config
        self.saved.append((config.name, replacing_name))

    def delete_superkey(self, name: str) -> bool:
        self.deleted.append(name)
        return self.values.pop(name, None) is not None


@dataclass
class _Profiles:
    renamed: list[tuple[str, str]] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)

    def rename_superkey_references(self, old_name: str, new_name: str) -> int:
        self.renamed.append((old_name, new_name))
        return 1

    def replace_superkey_with_suppress(self, superkey_name: str) -> int:
        self.replaced.append(superkey_name)
        return 1


def test_persistence_renames_with_one_store_write_and_cleans_profile_after_delete() -> None:
    store = _Store(values={"Old": SuperkeyConfig(name="Old")})
    persistence = SuperkeyPersistence(store)
    config = SuperkeyConfig(name="New")
    profiles = _Profiles()

    persistence.save(config, replacing_name="Old", profiles=profiles)
    assert persistence.delete("New", profiles=profiles) is True

    assert store.saved == [("New", "Old")]
    assert store.deleted == ["New"]
    assert profiles.renamed == [("Old", "New")]
    assert profiles.replaced == ["New"]


def test_persistence_reports_name_collision() -> None:
    store = _Store(
        values={
            "Old": SuperkeyConfig(name="Old"),
            "Taken": SuperkeyConfig(name="Taken"),
        }
    )

    profiles = _Profiles()
    with pytest.raises(ValueError, match="already exists"):
        SuperkeyPersistence(store).save(
            SuperkeyConfig(name="Taken"),
            replacing_name="Old",
            profiles=profiles,
        )

    assert store.saved == []
    assert profiles.renamed == []


def test_persistence_does_not_mutate_profiles_when_store_delete_fails() -> None:
    profiles = _Profiles()

    assert SuperkeyPersistence(_Store()).delete("Missing", profiles=profiles) is False

    assert profiles.replaced == []
