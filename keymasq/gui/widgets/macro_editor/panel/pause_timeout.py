"""Shared timeout control for macro playback and child calls."""

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]


class PauseTimeoutControl(Gtk.Box):
    def __init__(self, on_changed: Callable[[float], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._on_changed = on_changed
        self._syncing = False
        self.append(Gtk.Label(label="Discard paused playback after:"))
        self.seconds = Gtk.SpinButton.new_with_range(1, 2_147_483_647, 1)
        self.seconds.set_digits(0)
        self.seconds.set_width_chars(6)
        self.seconds.set_value(60)
        self.append(self.seconds)
        self.append(Gtk.Label(label="seconds"))
        self.never = Gtk.CheckButton(label="Never")
        self.append(self.never)
        self.set_tooltip_text(
            "Time since trigger release. Expiry cancels this macro and its children, "
            "including their active mouse moves and waitable commands. "
            "The parent and siblings keep their own behavior. Resuming resets the timer."
        )
        self.set_timeout(0)
        self.seconds.connect("value-changed", self._changed)
        self.never.connect("toggled", self._changed)

    def get_timeout(self) -> float:
        return 0.0 if self.never.get_active() else self.seconds.get_value()

    def set_timeout(self, seconds: float) -> None:
        self._syncing = True
        try:
            self.never.set_active(seconds <= 0)
            self.seconds.set_value(seconds if seconds > 0 else 60)
            self.seconds.set_sensitive(seconds > 0)
        finally:
            self._syncing = False

    def _changed(self, _widget: Gtk.Widget) -> None:
        if self._syncing:
            return
        self.seconds.set_sensitive(not self.never.get_active())
        self._on_changed(self.get_timeout())
