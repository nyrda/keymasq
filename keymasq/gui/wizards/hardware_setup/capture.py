from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.devices import primary_input_class

SessionRequestAsync = Callable[[dict, Callable[[dict | None], bool]], object]


def _ignore_capture_end_response(_response: dict | None) -> bool:
    return False


def make_capture_status_row(status_label: Gtk.Label) -> tuple[Gtk.Box, Gtk.Widget]:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    row.set_halign(Gtk.Align.START)
    row.set_margin_top(12)
    dot = Gtk.Box()
    dot.add_css_class("capture-recording-dot")
    dot.set_size_request(10, 10)
    dot.set_valign(Gtk.Align.CENTER)
    dot.set_visible(False)
    row.append(dot)
    row.append(status_label)
    return row, dot


def set_capture_status(
    status_label: Gtk.Label,
    dot: Gtk.Widget,
    text: object,
    *,
    recording: bool = False,
) -> None:
    status_label.set_label(str(text))
    dot.set_visible(recording)


def update_capture_ui(owner) -> None:
    if owner.current_button_index >= len(owner.button_definitions):
        owner._finish_capture()
        return

    btn = owner.button_definitions[owner.current_button_index]
    owner.capture_title.set_label(f"Capturing: {btn['label']}")

    if btn["type"] == "wheel":
        owner.capture_instruction.set_label("Scroll UP on your device")
    elif btn["type"] == "wheel_h":
        direction = "RIGHT" if btn["evdev_value"] > 0 else "LEFT"
        owner.capture_instruction.set_label(f"Scroll {direction} on your device")
    else:
        owner.capture_instruction.set_label("Press this button on your device")

    set_capture_status(
        owner.capture_status,
        owner.capture_status_dot,
        "Recording button presses..."
        if owner._capturing
        else "Click 'Start Capture' then perform the action",
        recording=owner._capturing,
    )

    progress = (
        owner.current_button_index / len(owner.button_definitions)
        if owner.button_definitions
        else 0
    )
    owner.capture_progress.set_fraction(progress)


def clear_captured_list(owner) -> None:
    while row := owner.captured_list.get_row_at_index(0):
        owner.captured_list.remove(row)


def add_captured_button(owner, btn_def: dict, evdev_code: str) -> None:
    row = Gtk.ListBoxRow()
    row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    row_box.set_margin_top(8)
    row_box.set_margin_bottom(8)
    row_box.set_margin_start(12)
    row_box.set_margin_end(12)

    label = Gtk.Label(label=btn_def["label"])
    row_box.append(label)

    evdev_label = Gtk.Label(label=f"→ {evdev_code}")
    evdev_label.add_css_class("dim-label")
    row_box.append(evdev_label)

    check = Gtk.Image(icon_name="emblem-ok-symbolic")
    row_box.append(check)

    row.set_child(row_box)
    owner.captured_list.append(row)


def on_start_capture(owner, session_request_async: SessionRequestAsync) -> None:
    if owner._capturing:
        return

    if owner.current_button_index >= len(owner.button_definitions):
        return

    owner._capturing = True
    owner.capture_btn.set_label("Listening...")
    owner.capture_btn.set_sensitive(False)
    set_capture_status(
        owner.capture_status,
        owner.capture_status_dot,
        "Recording button presses...",
        recording=True,
    )
    owner._capture_remaining_ids = [
        btn["id"] for btn in owner.button_definitions[owner.current_button_index :]
    ]
    capture_interfaces = [
        iface
        for iface in owner.discovered_interfaces.values()
        if str(
            iface.get("config_path")
            or iface.get("stable_path")
            or iface.get("path")
            or ""
        )
    ]
    session_request_async(
        {
            "command": "begin_capture",
            "hardware_id": owner._capture_hardware_id,
            "end_on_disconnect": True,
            "evdev_paths": [
                str(
                    iface.get("config_path")
                    or iface.get("stable_path")
                    or iface.get("path")
                    or ""
                )
                for iface in capture_interfaces
            ],
            "evdev_interfaces": [
                {
                    "id": str(iface.get("id", "") or ""),
                    "path": str(
                        iface.get("config_path")
                        or iface.get("stable_path")
                        or iface.get("path")
                        or ""
                    ),
                    "type": str(primary_input_class(iface.get("device_types")).value),
                    "phys": str(iface.get("phys", "") or ""),
                    "capabilities": list(iface.get("capabilities", [])),
                }
                for iface in capture_interfaces
            ],
        },
        owner._on_capture_begin_response,
    )


def on_capture_begin_response(owner, result: dict | None, glib) -> bool:
    if not owner._capturing:
        return False

    if not result or result.get("status") != "ok":
        set_capture_status(
            owner.capture_status,
            owner.capture_status_dot,
            (result or {}).get("message", "Capture failed: session unavailable"),
        )
        owner._stop_capture()
        return False

    warnings = result.get("warnings") or []
    if warnings:
        set_capture_status(
            owner.capture_status,
            owner.capture_status_dot,
            f"Capture warnings: {', '.join(str(w) for w in warnings)}",
        )

    owner._capture_poll_id = glib.timeout_add(16, owner._poll_capture)
    return False


def poll_capture(owner, session_request_async: SessionRequestAsync) -> bool:
    if not owner._capturing:
        return False

    if owner._capture_poll_inflight:
        return True

    owner._capture_poll_inflight = True
    session_request_async(
        {
            "command": "capture_read",
            "hardware_id": owner._capture_hardware_id,
        },
        owner._on_capture_poll_response,
    )
    return True


def on_capture_poll_response(owner, result: dict | None) -> bool:
    owner._capture_poll_inflight = False
    if not owner._capturing:
        return False

    if not result:
        return False

    if result.get("status") != "ok":
        set_capture_status(
            owner.capture_status,
            owner.capture_status_dot,
            result.get("message", "Capture failed"),
        )
        owner._stop_capture()
        return False

    captured = result.get("captured")
    if not isinstance(captured, dict):
        return True

    if owner.current_button_index >= len(owner.button_definitions):
        owner._finish_capture()
        return False

    btn_def = owner.button_definitions[owner.current_button_index]
    btn_def["evdev"] = captured.get("evdev", "unknown")
    btn_def["evdev_code"] = captured.get("code")
    btn_def["evdev_value"] = captured.get("value")
    btn_def["source"] = captured.get("source")
    btn_def["stable_path"] = captured.get("stable_path")

    evdev_display = str(captured.get("evdev", "unknown"))
    if captured.get("direction"):
        evdev_display = f"{evdev_display} ({captured.get('direction')})"
    if captured.get("source"):
        evdev_display = f"{evdev_display} [{captured.get('source')}]"

    add_captured_button(owner, btn_def, evdev_display)
    owner.current_button_index += 1
    remaining = max(0, len(owner.button_definitions) - owner.current_button_index)
    set_capture_status(
        owner.capture_status,
        owner.capture_status_dot,
        f"Recording button presses... Captured {evdev_display} ({remaining} remaining)",
        recording=True,
    )

    if remaining == 0:
        owner._finish_capture()
        return False

    owner._update_capture_ui()
    return False


def stop_capture(owner, session_request_async: SessionRequestAsync, glib) -> None:
    owner._capturing = False

    if owner._capture_hardware_id:
        session_request_async(
            {
                "command": "end_capture",
                "hardware_id": owner._capture_hardware_id,
            },
            _ignore_capture_end_response,
        )
        owner._capture_hardware_id = None

    if owner._capture_poll_id:
        glib.source_remove(owner._capture_poll_id)
        owner._capture_poll_id = None
    owner._capture_poll_inflight = False

    owner.capture_btn.set_label("Start Capture")
    owner.capture_btn.set_sensitive(True)
    owner.capture_status_dot.set_visible(False)


def on_skip(owner) -> None:
    owner._stop_capture()

    if owner.current_button_index >= len(owner.button_definitions):
        return

    btn_def = owner.button_definitions[owner.current_button_index]
    btn_def["evdev"] = "unknown"
    btn_def["skipped"] = True
    add_captured_button(owner, btn_def, "(skipped)")

    owner.current_button_index += 1
    owner._update_capture_ui()


def finish_capture(owner) -> None:
    owner._stop_capture()
    owner.capture_title.set_label("Setup Complete!")
    owner.capture_instruction.set_label("All buttons captured")
    set_capture_status(
        owner.capture_status,
        owner.capture_status_dot,
        "Click Save to finish",
    )
    owner.capture_progress.set_fraction(1.0)
    owner.capture_btn.set_label("Save")
    owner.capture_btn.set_sensitive(True)
    owner.skip_btn.set_visible(False)

    owner.capture_btn.disconnect_by_func(owner._on_start_capture)
    owner.capture_btn.connect("clicked", owner._on_save)
