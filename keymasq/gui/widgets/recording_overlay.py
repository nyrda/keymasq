import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.session_client import session_request_async


class RecordingOverlay(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._window = window
        self._start_ms: int = 0
        self._event_count: int = 0
        self._timer_id: int = 0
        self._stop_btn: Gtk.Button | None = None
        self._status_label: Gtk.Label | None = None
        self.add_css_class("recording-overlay")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._build_ui()

    def _build_ui(self) -> None:
        self.set_halign(Gtk.Align.FILL)
        self.set_valign(Gtk.Align.FILL)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        panel.add_css_class("recording-overlay-panel")
        panel.set_halign(Gtk.Align.CENTER)
        panel.set_valign(Gtk.Align.CENTER)
        panel.set_size_request(420, -1)
        panel.set_margin_top(24)
        panel.set_margin_bottom(24)
        panel.set_margin_start(24)
        panel.set_margin_end(24)
        self.append(panel)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.set_halign(Gtk.Align.CENTER)
        panel.append(header)

        dot = Gtk.Box()
        dot.add_css_class("recording-overlay-dot")
        dot.set_size_request(12, 12)
        dot.set_valign(Gtk.Align.CENTER)
        header.append(dot)

        title = Gtk.Label(label="Recording macro")
        title.add_css_class("title-2")
        title.set_valign(Gtk.Align.CENTER)
        header.append(title)

        body = Gtk.Label(label="Input is being captured until recording is stopped.")
        body.add_css_class("dim-label")
        body.set_wrap(True)
        body.set_justify(Gtk.Justification.CENTER)
        body.set_halign(Gtk.Align.CENTER)
        panel.append(body)

        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        stats.add_css_class("recording-overlay-stats")
        stats.set_halign(Gtk.Align.CENTER)
        panel.append(stats)

        duration_stat, self._duration_label = self._build_stat("Duration", "00:00.000")
        stats.append(duration_stat)

        events_stat, self._events_label = self._build_stat("Events", "0")
        stats.append(events_stat)

        stop_btn = Gtk.Button(label="Stop Recording")
        stop_btn.add_css_class("destructive-action")
        stop_btn.add_css_class("recording-stop-button")
        stop_btn.set_halign(Gtk.Align.CENTER)
        stop_btn.set_size_request(220, 48)
        stop_btn.connect("clicked", self._on_stop_clicked)
        self._stop_btn = stop_btn
        panel.append(stop_btn)

        status_label = Gtk.Label()
        status_label.add_css_class("caption")
        status_label.add_css_class("error")
        status_label.set_wrap(True)
        status_label.set_halign(Gtk.Align.CENTER)
        status_label.set_visible(False)
        self._status_label = status_label
        panel.append(status_label)

    def _build_stat(self, title: str, value: str) -> tuple[Gtk.Box, Gtk.Label]:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        box.add_css_class("recording-overlay-stat")

        title_label = Gtk.Label(label=title)
        title_label.add_css_class("caption")
        title_label.add_css_class("dim-label")
        title_label.set_halign(Gtk.Align.CENTER)
        box.append(title_label)

        value_label = Gtk.Label(label=value)
        value_label.add_css_class("title-3")
        value_label.set_halign(Gtk.Align.CENTER)
        box.append(value_label)
        return box, value_label

    def on_started(self, data: dict) -> None:
        import time

        self._start_ms = int(time.monotonic() * 1000)
        self._event_count = 0
        self._duration_label.set_label("00:00.000")
        self._events_label.set_label("0")
        self._hide_status()
        if self._stop_btn:
            self._stop_btn.set_label("Stop Recording")
            self._stop_btn.set_sensitive(True)
        if self._timer_id:
            GLib.source_remove(self._timer_id)
        self._timer_id = GLib.timeout_add(100, self._update_timer)

    def on_progress(self, data: dict) -> None:
        duration_ms = data.get("duration_ms", 0)
        self._event_count = data.get("event_count", self._event_count)
        self._update_display(duration_ms)

    def on_stopped(self) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0
        if self._stop_btn:
            self._stop_btn.set_label("Stop Recording")
            self._stop_btn.set_sensitive(True)
        self._hide_status()

    def _update_timer(self) -> bool:
        import time

        if not self.get_visible():
            self._timer_id = 0
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
        self._events_label.set_label(str(self._event_count))

    def _on_stop_clicked(self, btn: Gtk.Button) -> None:
        self._hide_status()
        btn.set_sensitive(False)
        btn.set_label("Stopping...")
        session_request_async(
            {"command": "stop_recording"},
            lambda result: self._on_stop_done(btn, result),
        )

    def _on_stop_done(self, btn: Gtk.Button, result: dict | None) -> bool:
        if result and result.get("status") == "ok":
            if self._timer_id:
                GLib.source_remove(self._timer_id)
                self._timer_id = 0
            btn.set_sensitive(True)
            btn.set_label("Stop Recording")
            return False

        btn.set_sensitive(True)
        btn.set_label("Stop Recording")
        self._ensure_timer_running()
        self._show_status(str((result or {}).get("message") or "Failed to stop recording."))
        return False

    def _ensure_timer_running(self) -> None:
        if self._timer_id or not self._start_ms or not self.get_visible():
            return
        self._timer_id = GLib.timeout_add(100, self._update_timer)

    def _show_status(self, message: str) -> None:
        if self._status_label is None:
            return
        self._status_label.set_label(message)
        self._status_label.set_visible(True)

    def _hide_status(self) -> None:
        if self._status_label is not None:
            self._status_label.set_visible(False)
