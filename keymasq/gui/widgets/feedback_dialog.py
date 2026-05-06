from __future__ import annotations

import json
import os
import platform
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq import __version__
from keymasq.gui.session_client import GuiTaskResult, run_gui_task

DEFAULT_FEEDBACK_ENDPOINT = "https://feedback.keymasq.tools/api/feedback"
OS_RELEASE_PATH = Path("/etc/os-release")


@dataclass(frozen=True)
class FeedbackSubmissionResult:
    ok: bool
    message: str


def feedback_endpoint() -> str:
    return os.environ.get("KEYMASQ_FEEDBACK_URL", DEFAULT_FEEDBACK_ENDPOINT).strip()


def linux_distribution_name(os_release_path: Path = OS_RELEASE_PATH) -> str:
    try:
        with os_release_path.open(encoding="utf-8") as file:
            values = {}
            for line in file:
                key, value = _parse_os_release_line(line)
                if key:
                    values[key] = value
    except OSError:
        return "unknown"

    return values.get("PRETTY_NAME") or values.get("NAME") or values.get("ID") or "unknown"


def _parse_os_release_line(line: str) -> tuple[str, str]:
    key, separator, raw_value = line.strip().partition("=")
    if not separator or not key or key.startswith("#"):
        return "", ""
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


def submit_feedback(endpoint: str, payload: dict[str, Any]) -> FeedbackSubmissionResult:
    if not endpoint:
        return FeedbackSubmissionResult(False, "Feedback is not configured for this build.")

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"keymasq-gui/{__version__}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=8.0) as response:
            response.read(4096)
            if 200 <= response.status < 300:
                return FeedbackSubmissionResult(True, "Thanks for the feedback.")
    except urllib.error.HTTPError as exc:
        exc.read(4096)
        if exc.code == 429:
            return FeedbackSubmissionResult(
                False,
                "Please wait before sending more feedback.",
            )
        if exc.code == 400:
            return FeedbackSubmissionResult(
                False,
                "Add a little more detail before sending.",
            )
    except urllib.error.URLError:
        pass
    except TimeoutError:
        pass

    return FeedbackSubmissionResult(
        False,
        "Feedback could not be sent right now.",
    )


class FeedbackDialog(Adw.Dialog):
    def __init__(self, parent: Gtk.Window, endpoint: str | None = None) -> None:
        super().__init__(title="Feedback", content_width=480, content_height=520)
        self._parent = parent
        self._endpoint = endpoint if endpoint is not None else feedback_endpoint()
        self._submit_inflight = False
        self._sent = False

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)

        category_label = Gtk.Label(label="Type", xalign=0)
        category_label.add_css_class("caption")
        content.append(category_label)

        self.category_dropdown = Gtk.DropDown.new_from_strings(
            ["Bug", "Idea", "Question", "Other"]
        )
        self.category_dropdown.set_selected(2)
        content.append(self.category_dropdown)

        message_label = Gtk.Label(label="Message", xalign=0)
        message_label.add_css_class("caption")
        content.append(message_label)

        message_frame = Gtk.Frame()
        message_frame.set_vexpand(True)
        self.message_view = Gtk.TextView()
        self.message_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.message_view.set_vexpand(True)
        self.message_view.set_left_margin(8)
        self.message_view.set_right_margin(8)
        self.message_view.set_top_margin(8)
        self.message_view.set_bottom_margin(8)
        self.message_view.get_buffer().connect("changed", self._on_message_changed)
        message_frame.set_child(self.message_view)
        content.append(message_frame)

        self.diagnostics_check = Gtk.CheckButton(label="Include Diagnostics")
        self.diagnostics_check.set_active(True)
        self.diagnostics_check.set_tooltip_text(
            "Sends Keymasq version, Linux distribution, platform, desktop/session type, "
            "and configured device names."
        )
        content.append(self.diagnostics_check)

        contact_label = Gtk.Label(label="Contact", xalign=0)
        contact_label.add_css_class("caption")
        content.append(contact_label)

        self.contact_entry = Gtk.Entry()
        self.contact_entry.set_placeholder_text("optional")
        content.append(self.contact_entry)

        self.status_label = Gtk.Label(label="", xalign=0)
        self.status_label.set_wrap(True)
        self.status_label.add_css_class("caption")
        self.status_label.set_visible(False)
        content.append(self.status_label)

        toolbar.set_content(content)
        toolbar.add_bottom_bar(self._build_footer())
        self.set_child(toolbar)
        self._refresh_submit_state()

    def _build_footer(self) -> Gtk.Widget:
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        footer.set_margin_top(8)
        footer.set_margin_bottom(8)
        footer.set_margin_start(12)
        footer.set_margin_end(12)

        close_btn = Gtk.Button(label="Cancel")
        close_btn.connect("clicked", self._on_close_clicked)
        footer.append(close_btn)

        self.submit_btn = Gtk.Button(label="Send")
        self.submit_btn.add_css_class("suggested-action")
        self.submit_btn.connect("clicked", self._on_submit_clicked)
        footer.append(self.submit_btn)

        return footer

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _on_message_changed(self, _buffer) -> None:
        self._refresh_submit_state()

    def _refresh_submit_state(self) -> None:
        self.submit_btn.set_sensitive(bool(self._message_text()) and not self._submit_inflight)

    def _message_text(self) -> str:
        buffer = self.message_view.get_buffer()
        start, end = buffer.get_bounds()
        return buffer.get_text(start, end, True).strip()

    def _selected_category(self) -> str:
        item = self.category_dropdown.get_selected_item()
        if item is None:
            return "Other"
        value = getattr(item, "get_string", lambda: "Other")()
        return str(value).strip() or "Other"

    def _payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": "keymasq-gui",
            "category": self._selected_category(),
            "message": self._message_text(),
            "contact": self.contact_entry.get_text().strip(),
        }

        if self.diagnostics_check.get_active():
            payload.update(
                {
                    "app_version": __version__,
                    "distribution": linux_distribution_name(),
                    "platform": platform.platform(),
                    "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
                    "session_type": os.environ.get("XDG_SESSION_TYPE", ""),
                    "devices": self._diagnostic_device_names(),
                }
            )
        return payload

    def _diagnostic_device_names(self) -> list[str]:
        devices: dict[str, str] = {}
        stack = getattr(self._parent, "stack", None)
        child = stack.get_first_child() if stack is not None else None
        while child is not None:
            device = getattr(child, "device", None)
            self._add_diagnostic_device(devices, device)
            child = child.get_next_sibling()

        hardware_manager = getattr(self._parent, "hardware_manager", None)
        list_hardware = getattr(hardware_manager, "list_hardware", None)
        if callable(list_hardware):
            try:
                loaded_devices = list_hardware()
            except Exception:
                pass
            else:
                if isinstance(loaded_devices, Iterable):
                    for device in loaded_devices:
                        self._add_diagnostic_device(devices, device)

        return [f"{name} ({hardware_id})" for hardware_id, name in sorted(devices.items())]

    def _add_diagnostic_device(self, devices: dict[str, str], device: object) -> None:
        if device is None:
            return
        hardware_id = str(getattr(device, "hardware_id", "") or "").strip()
        name = str(getattr(device, "name", "") or "").strip()
        if not hardware_id and not name:
            return
        if not hardware_id:
            hardware_id = name
        devices.setdefault(hardware_id, name or hardware_id)

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self.status_label.set_text(message)
        self.status_label.set_visible(True)
        if error:
            self.status_label.add_css_class("error")
        else:
            self.status_label.remove_css_class("error")

    def _on_submit_clicked(self, _button: Gtk.Button) -> None:
        if self._submit_inflight or self._sent:
            return

        payload = self._payload()
        if not payload["message"]:
            self._refresh_submit_state()
            return

        self._submit_inflight = True
        self.submit_btn.set_sensitive(False)
        self.submit_btn.set_label("Sending...")
        self._set_status("")

        endpoint = self._endpoint

        def worker() -> FeedbackSubmissionResult:
            return submit_feedback(endpoint, payload)

        run_gui_task(worker, self._on_submit_finished)

    def _on_submit_finished(
        self,
        result: GuiTaskResult[FeedbackSubmissionResult],
    ) -> bool:
        self._submit_inflight = False
        submission = result.value if result.ok else None

        if submission is not None and submission.ok:
            self._sent = True
            self.submit_btn.set_label("Sent")
            self.submit_btn.set_sensitive(False)
            self.message_view.set_sensitive(False)
            self.category_dropdown.set_sensitive(False)
            self.contact_entry.set_sensitive(False)
            self.diagnostics_check.set_sensitive(False)
            self._set_status(submission.message)
            return False

        self.submit_btn.set_label("Send")
        self._refresh_submit_state()
        message = (
            submission.message
            if submission is not None
            else "Feedback could not be sent right now."
        )
        self._set_status(message, error=True)
        return False
