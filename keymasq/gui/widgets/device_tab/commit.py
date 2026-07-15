"""Deferred selector-commit state and dialog-close coordination."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import gi

gi.require_version("Adw", "1")

from gi.repository import Adw  # pyright: ignore[reportAttributeAccessIssue]

type Commit = Callable[[], None]


@dataclass(slots=True)
class DeferredCommitState:
    """UI-independent queue state for edits delayed until dialog teardown."""

    source_id: int = 0
    pending: Commit | None = None

    def queue(self, commit: Commit) -> None:
        previous = self.pending
        if previous is None:
            self.pending = commit
            return

        def combined() -> None:
            previous()
            commit()

        self.pending = combined

    def take(self) -> Commit | None:
        commit = self.pending
        self.pending = None
        return commit

    def clear_source(self) -> int:
        source_id = self.source_id
        self.source_id = 0
        return source_id


class SelectorCommitMixin:
    def _defer_selector_commit_until_dialog_closed(
        self: Any,
        dialog: Adw.Dialog,
    ) -> Callable[[Commit], None]:
        pending_commit: Commit | None = None

        def set_pending_commit(commit: Commit) -> None:
            nonlocal pending_commit
            pending_commit = commit

        def on_dialog_closed(_dialog: Adw.Dialog) -> None:
            if pending_commit is not None:
                self._queue_selector_commit_after_close(pending_commit)

        dialog.connect("closed", on_dialog_closed)
        return set_pending_commit

    def _queue_selector_commit_after_close(self: Any, commit: Commit) -> None:
        source_id = self._commit_state.clear_source()
        if source_id:
            self._remove_selector_commit_source(source_id)
        self._commit_state.queue(commit)
        self._commit_state.source_id = self._schedule_selector_commit(
            self._run_pending_selector_commit
        )

    def _run_pending_selector_commit(self: Any) -> bool:
        self._commit_state.clear_source()
        commit = self._commit_state.take()
        if commit is not None:
            commit()
        return False

    def _on_device_tab_destroy(self: Any, _widget) -> None:
        source_id = self._commit_state.clear_source()
        if source_id:
            self._remove_selector_commit_source(source_id)
        self._run_pending_selector_commit()
