from keymasq.session.wayland_protocols._active_window_tracker import ActiveWindowTracker

WLR_TOPLEVEL_STATE_ACTIVATED = 2


class WlrForeignToplevelManagerTracker(ActiveWindowTracker):
    def __init__(self) -> None:
        super().__init__(WLR_TOPLEVEL_STATE_ACTIVATED)
