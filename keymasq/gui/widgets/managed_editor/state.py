"""UI-independent state for managed-resource editors."""

from dataclasses import dataclass
from enum import Enum, auto


class EditorSelectionKind(Enum):
    """The two rows an editor can select."""

    NEW_ITEM = auto()
    SAVED_ITEM = auto()


@dataclass(frozen=True, slots=True)
class EditorSelection:
    """Stable identity for either the new-item row or a saved item."""

    kind: EditorSelectionKind
    item_id: str | None

    def __post_init__(self) -> None:
        if self.kind is EditorSelectionKind.NEW_ITEM:
            if self.item_id is not None:
                raise ValueError("The new-item selection cannot have an item id")
            return
        if not self.item_id:
            raise ValueError("A saved-item selection requires a non-empty item id")

    @classmethod
    def new_item(cls) -> "EditorSelection":
        return cls(EditorSelectionKind.NEW_ITEM, None)

    @classmethod
    def saved_item(cls, item_id: str) -> "EditorSelection":
        return cls(EditorSelectionKind.SAVED_ITEM, item_id)

    @property
    def is_new_item(self) -> bool:
        return self.kind is EditorSelectionKind.NEW_ITEM


@dataclass(frozen=True, slots=True)
class PendingEditorTransition:
    """A guarded selection transition waiting for the user's decision."""

    selection: EditorSelection | None
    restart_new_item: bool


class EditorState:
    """Tracks dirty state and guarded selection transitions."""

    __slots__ = (
        "_active_selection",
        "_is_dirty",
        "_pending_transition",
        "_selection_sync_depth",
    )

    def __init__(self) -> None:
        self._is_dirty = False
        self._active_selection: EditorSelection | None = None
        self._pending_transition: PendingEditorTransition | None = None
        self._selection_sync_depth = 0

    @property
    def is_dirty(self) -> bool:
        return self._is_dirty

    @property
    def active_selection(self) -> EditorSelection | None:
        return self._active_selection

    @property
    def pending_transition(self) -> PendingEditorTransition | None:
        return self._pending_transition

    @property
    def selection_guard_suppressed(self) -> bool:
        """Whether a list selection change is part of an internal UI sync."""

        return self._selection_sync_depth > 0

    def mark_dirty(self) -> None:
        self._is_dirty = True

    def mark_clean(self) -> None:
        self._is_dirty = False

    def activate(self, selection: EditorSelection | None) -> None:
        self._active_selection = selection

    def selection_change_needs_confirmation(
        self,
        target: EditorSelection | None,
    ) -> bool:
        """Return whether moving to ``target`` would abandon a dirty draft."""

        return (
            self._is_dirty
            and self._active_selection is not None
            and target != self._active_selection
        )

    def new_draft_restart_needs_confirmation(self, *, pristine_draft: bool) -> bool:
        """Return whether restarting the new-item draft needs confirmation."""

        if not self._is_dirty:
            return False
        active = self._active_selection
        if active is None or not active.is_new_item:
            return True
        return not pristine_draft

    def queue_transition(
        self,
        selection: EditorSelection | None,
        *,
        restart_new_item: bool = False,
    ) -> None:
        if restart_new_item and (selection is None or not selection.is_new_item):
            raise ValueError("A new-item restart must target the new-item selection")
        self._pending_transition = PendingEditorTransition(
            selection=selection,
            restart_new_item=restart_new_item,
        )

    def take_pending_transition(self) -> PendingEditorTransition | None:
        """Remove and return the pending transition as one atomic operation."""

        pending = self._pending_transition
        self._pending_transition = None
        return pending

    def clear_pending_transition(self) -> None:
        self._pending_transition = None

    def begin_selection_sync(self) -> None:
        """Suppress selection guards during filtering or programmatic restoration."""

        self._selection_sync_depth += 1

    def end_selection_sync(self) -> None:
        if self._selection_sync_depth == 0:
            raise RuntimeError("Selection sync was not active")
        self._selection_sync_depth -= 1
