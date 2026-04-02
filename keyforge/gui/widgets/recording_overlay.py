import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib, Gtk

from keyforge.gui.session_client import session_request_async


class RecordingOverlay(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._window = window
        self._start_ms: int = 0
        self._event_count: int = 0
        self._timer_id: int = 0
        self.add_css_class("card")
        self.set_margin_top(8)
        self.set_margin_end(8)
        self._build_ui()

    def _build_ui(self) -> None:
        self.set_margin_top(10)
        self.set_margin_bottom(10)
        self.set_margin_start(14)
        self.set_margin_end(14)

        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        indicator_label = Gtk.Label(label="⏺ Recording")
        indicator_label.add_css_class("heading")
        indicator_label.set_hexpand(True)
        top_row.append(indicator_label)

        stop_btn = Gtk.Button(label="Stop")
        stop_btn.add_css_class("destructive-action")
        stop_btn.add_css_class("flat")
        stop_btn.connect("clicked", self._on_stop_clicked)
        top_row.append(stop_btn)

        self.append(top_row)

        self._duration_label = Gtk.Label(label="00:00.000")
        self._duration_label.add_css_class("caption")
        self._duration_label.add_css_class("dim-label")
        self._duration_label.set_halign(Gtk.Align.START)
        self.append(self._duration_label)

        self._events_label = Gtk.Label(label="0 events")
        self._events_label.add_css_class("caption")
        self._events_label.add_css_class("dim-label")
        self._events_label.set_halign(Gtk.Align.START)
        self.append(self._events_label)

    def on_started(self, data: dict) -> None:
        import time

        self._start_ms = int(time.monotonic() * 1000)
        self._event_count = 0
        self._duration_label.set_label("00:00.000")
        self._events_label.set_label("0 events")
        if self._timer_id:
            GLib.source_remove(self._timer_id)
        self._timer_id = GLib.timeout_add(100, self._update_timer)

    def on_progress(self, data: dict) -> None:
        duration_ms = data.get("duration_ms", 0)
        self._event_count = data.get("event_count", self._event_count)
        self._update_display(duration_ms)

    def _update_timer(self) -> bool:
        import time

        if not self.get_visible():
            return False
        elapsed_ms = int(time.monotonic() * 1000) - self._start_ms
        self._update_display(elapsed_ms)
        return True

    def _update_display(self, duration_ms: int) -> None:
        total_s = duration_ms // 1000
        ms = duration_ms % 1000
        minutes = total_s // 60
        seconds = total_s % 60
        self._duration_label.set_label(f"{minutes:02d}:{seconds:02d}.{ms:03d}")
        self._events_label.set_label(f"{self._event_count} events")

    def _on_stop_clicked(self, btn: Gtk.Button) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0
        btn.set_sensitive(False)
        session_request_async(
            {"command": "stop_recording"},
            lambda _result: self._on_stop_done(btn),
        )

    def _on_stop_done(self, btn: Gtk.Button) -> bool:
        btn.set_sensitive(True)
        return False
