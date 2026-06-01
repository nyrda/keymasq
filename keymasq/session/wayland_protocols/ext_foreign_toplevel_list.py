from keymasq.session.wayland_protocols._active_window_tracker import ActiveWindowTracker

TOPLEVEL_STATE_ACTIVATED = 2


class ExtForeignToplevelListTracker(ActiveWindowTracker):
    def __init__(self) -> None:
        super().__init__(TOPLEVEL_STATE_ACTIVATED)

    def mark_done(self) -> None:
        self._mark_done()
