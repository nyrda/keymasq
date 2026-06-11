from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.devices import resolve_evdev_code
from keymasq.common.models import (
    AnalogAxisDefinition,
    AnalogInputDefinition,
    HardwareConfig,
)
from keymasq.gui.session_client import JsonDict
from keymasq.gui.widgets.device_tab.capture_helpers import (
    _make_capture_status_row,
    _set_capture_status,
    make_unlock_button_content,
)

SessionRequestAsync = Callable[[JsonDict, Callable[[JsonDict | None], bool]], object]


@dataclass(frozen=True)
class LearnAnalogResult:
    analog: AnalogInputDefinition
    source: str | None
    stable_path: str | None


class LearnAnalogFlow:
    def __init__(
        self,
        parent_window,
        session_client: SessionRequestAsync,
        hardware_config: HardwareConfig,
        on_complete: Callable[[LearnAnalogResult], None],
    ) -> None:
        self.parent_window = parent_window
        self._session_request_async = session_client
        self.hardware_config = hardware_config
        self._on_complete = on_complete
        self._poll_id: int | None = None
        self._poll_inflight = False
        self._capturing = False
        self._capture_active_hardware_id: str | None = None
        self._capture_generation = 0
        self._context: dict[str, object] = {}

    def present(self) -> None:
        dialog = Adw.Dialog(title="Learn Analog Input", content_width=520, content_height=-1)
        dialog.connect("closed", self._on_dialog_closed)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        info = Gtk.Label(
            label=(
                "Choose Generic Axis or Stick, start capture, then move the physical "
                "control through its full range."
            )
        )
        info.set_halign(Gtk.Align.START)
        info.set_wrap(True)
        box.append(info)

        form_grid = Gtk.Grid()
        form_grid.set_column_spacing(8)
        form_grid.set_row_spacing(8)
        box.append(form_grid)

        type_label = Gtk.Label(label="Type:")
        type_label.set_halign(Gtk.Align.END)
        type_label.set_valign(Gtk.Align.CENTER)
        type_dropdown = Gtk.DropDown.new_from_strings(["Generic Axis", "Stick"])
        type_dropdown.set_halign(Gtk.Align.START)
        form_grid.attach(type_label, 0, 0, 1, 1)
        form_grid.attach(type_dropdown, 1, 0, 1, 1)

        id_entry = Gtk.Entry()
        id_entry.set_text(self._next_analog_id("axis"))
        id_entry.set_hexpand(True)
        label_entry = Gtk.Entry()
        label_entry.set_text("Generic Axis")
        label_entry.set_hexpand(True)

        def on_type_changed(_dropdown, _param) -> None:
            if type_dropdown.get_selected() == 1:
                id_entry.set_text(self._next_analog_id("stick"))
                label_entry.set_text("Stick")
            else:
                id_entry.set_text(self._next_analog_id("axis"))
                label_entry.set_text("Generic Axis")

        type_dropdown.connect("notify::selected", on_type_changed)

        id_label = Gtk.Label(label="ID:")
        id_label.set_halign(Gtk.Align.END)
        id_label.set_valign(Gtk.Align.CENTER)
        form_grid.attach(id_label, 0, 1, 1, 1)
        form_grid.attach(id_entry, 1, 1, 1, 1)

        label_label = Gtk.Label(label="Label:")
        label_label.set_halign(Gtk.Align.END)
        label_label.set_valign(Gtk.Align.CENTER)
        form_grid.attach(label_label, 0, 2, 1, 1)
        form_grid.attach(label_entry, 1, 2, 1, 1)

        privilege_status = Gtk.Label(label="")
        privilege_status.add_css_class("dim-label")
        privilege_status.set_halign(Gtk.Align.START)
        privilege_status.set_wrap(True)
        box.append(privilege_status)

        status = Gtk.Label(label="")
        status.add_css_class("dim-label")
        status.set_halign(Gtk.Align.START)
        status.set_wrap(True)
        box.append(_make_capture_status_row(status))

        review_list = Gtk.ListBox()
        review_list.add_css_class("boxed-list")
        review_list.set_selection_mode(Gtk.SelectionMode.NONE)
        review_list.set_visible(False)
        box.append(review_list)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_close_dialog_clicked, dialog)
        btn_row.append(cancel_btn)

        start_btn = Gtk.Button(label="Start Capture")
        unlock_btn = Gtk.Button()
        unlock_btn.set_child(make_unlock_button_content("Unlock"))
        unlock_btn.set_tooltip_text(
            "Authorize raw original-input capture so Keymasq can detect analog axes before "
            "remapping."
        )
        unlock_btn.connect(
            "clicked",
            self._on_unlock_clicked,
            start_btn,
            privilege_status,
            status,
        )
        btn_row.append(unlock_btn)

        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.set_sensitive(False)
        save_btn.set_visible(False)
        save_btn.connect(
            "clicked",
            self._on_save_clicked,
            dialog,
            type_dropdown,
            id_entry,
            label_entry,
            review_list,
            status,
        )

        start_btn.connect(
            "clicked",
            self._on_start_clicked,
            dialog,
            type_dropdown,
            id_entry,
            label_entry,
            review_list,
            status,
            save_btn,
            unlock_btn,
            privilege_status,
        )
        btn_row.append(start_btn)
        btn_row.append(save_btn)
        box.append(btn_row)

        self._context = {
            "dialog": dialog,
            "type_dropdown": type_dropdown,
            "id_entry": id_entry,
            "label_entry": label_entry,
            "review_list": review_list,
            "status": status,
            "start_btn": start_btn,
            "save_btn": save_btn,
            "unlock_btn": unlock_btn,
            "privilege_status": privilege_status,
            "candidates": {},
        }
        self._update_capture_controls(start_btn, unlock_btn, privilege_status)
        dialog.set_child(box)
        dialog.present(self.parent_window)

    def _on_start_clicked(
        self,
        start_btn: Gtk.Button,
        dialog: Adw.Dialog,
        type_dropdown: Gtk.DropDown,
        id_entry: Gtk.Entry,
        label_entry: Gtk.Entry,
        review_list: Gtk.ListBox,
        status: Gtk.Label,
        save_btn: Gtk.Button,
        unlock_btn: Gtk.Button,
        privilege_status: Gtk.Label,
    ) -> None:
        if self._capturing:
            self.stop_capture()
            self._populate_review(
                type_dropdown,
                review_list,
                status,
                save_btn,
            )
            start_btn.set_label("Start Capture")
            save_btn.set_visible(save_btn.get_sensitive())
            self._update_capture_controls(
                start_btn,
                unlock_btn,
                privilege_status,
            )
            return

        _ = dialog, id_entry, label_entry
        capture_hardware_id = self.hardware_config.hardware_id
        self._capture_active_hardware_id = capture_hardware_id
        self._capture_generation += 1
        capture_generation = self._capture_generation
        self._context["candidates"] = {}
        review_list.set_visible(False)
        save_btn.set_sensitive(False)
        save_btn.set_visible(False)
        _set_capture_status(status, "Recording analog movement...", recording=True)
        start_btn.set_label("Review Capture")
        start_btn.set_sensitive(False)

        self._session_request_async(
            {
                "command": "begin_capture",
                "hardware_id": capture_hardware_id,
                "evdev_paths": [device.path for device in self.hardware_config.evdev_devices],
                "mode": "analog",
                "end_on_disconnect": True,
            },
            lambda result: self._on_capture_begun(
                result,
                capture_hardware_id,
                capture_generation,
                status,
                start_btn,
                unlock_btn,
                privilege_status,
            ),
        )

    def _on_capture_begun(
        self,
        result: JsonDict | None,
        expected_hardware_id: str | None,
        expected_generation: int,
        status: Gtk.Label,
        start_btn: Gtk.Button,
        unlock_btn: Gtk.Button,
        privilege_status: Gtk.Label,
    ) -> bool:
        if (
            self._capture_active_hardware_id is None
            or expected_hardware_id is None
            or self._capture_active_hardware_id != expected_hardware_id
            or self._capture_generation != expected_generation
        ):
            return False
        result_hardware_id = (result or {}).get("hardware_id")
        if result_hardware_id is not None and str(result_hardware_id) != expected_hardware_id:
            return False

        if not result or result.get("status") != "ok":
            _set_capture_status(status, (result or {}).get("message", "Capture failed"))
            self.stop_capture()
            start_btn.set_label("Start Capture")
            self._update_capture_controls(
                start_btn,
                unlock_btn,
                privilege_status,
            )
            return False

        self._capturing = True
        start_btn.set_sensitive(True)
        self._poll_id = GLib.timeout_add(16, self._poll_capture)
        return False

    def _poll_capture(self) -> bool:
        if not self._capturing:
            return False
        hardware_id = self._capture_active_hardware_id
        if hardware_id is None:
            self._capturing = False
            return False
        if self._poll_inflight:
            return True
        self._poll_inflight = True
        self._session_request_async(
            {
                "command": "capture_read",
                "hardware_id": hardware_id,
            },
            self._on_capture_read,
        )
        return True

    def _on_capture_read(self, result: JsonDict | None) -> bool:
        self._poll_inflight = False
        if not self._capturing or not result:
            return False
        if result.get("status") != "ok":
            status = self._context.get("status")
            if isinstance(status, Gtk.Label):
                _set_capture_status(
                    cast(Gtk.Label, status),
                    result.get("message", "Capture failed"),
                )
            self.stop_capture()
            start_btn = self._context.get("start_btn")
            unlock_btn = self._context.get("unlock_btn")
            privilege_status = self._context.get("privilege_status")
            if (
                isinstance(start_btn, Gtk.Button)
                and isinstance(unlock_btn, Gtk.Button)
                and isinstance(privilege_status, Gtk.Label)
            ):
                cast(Gtk.Button, start_btn).set_label("Start Capture")
                self._update_capture_controls(
                    cast(Gtk.Button, start_btn),
                    cast(Gtk.Button, unlock_btn),
                    cast(Gtk.Label, privilege_status),
                )
            return False
        captured = result.get("captured")
        if isinstance(captured, dict):
            self.record_candidate(cast(JsonDict, captured))
        return True

    def record_candidate(self, captured: JsonDict) -> None:
        code_raw = captured.get("code")
        value_raw = captured.get("value")
        try:
            code = int(cast(int, code_raw))
            value = int(cast(int, value_raw))
        except (TypeError, ValueError):
            return
        source = str(captured.get("source", "") or "")
        key = f"{source}:{code}"
        candidates = cast(
            dict[str, JsonDict],
            self._context.setdefault("candidates", {}),
        )
        candidate = candidates.get(key)
        if candidate is None:
            absinfo = captured.get("absinfo") if isinstance(captured.get("absinfo"), dict) else {}
            candidate = {
                "evdev": str(captured.get("evdev", "") or f"abs_{code}"),
                "code": code,
                "source": source,
                "stable_path": str(captured.get("stable_path", "") or ""),
                "rest": value,
                "minimum": int(cast(dict, absinfo).get("minimum", value)),
                "maximum": int(cast(dict, absinfo).get("maximum", value)),
                "observed_minimum": value,
                "observed_maximum": value,
                "count": 0,
            }
            candidates[key] = candidate
        candidate["observed_minimum"] = min(int(candidate["observed_minimum"]), value)
        candidate["observed_maximum"] = max(int(candidate["observed_maximum"]), value)
        candidate["minimum"] = min(int(candidate["minimum"]), value)
        candidate["maximum"] = max(int(candidate["maximum"]), value)
        candidate["count"] = int(candidate["count"]) + 1

        status = self._context.get("status")
        if isinstance(status, Gtk.Label):
            _set_capture_status(
                cast(Gtk.Label, status),
                f"Recording analog movement... Captured {len(candidates)} axes",
                recording=True,
            )

    def populate_review(
        self,
        type_dropdown: Gtk.DropDown,
        review_list: Gtk.ListBox,
        status: Gtk.Label,
        save_btn: Gtk.Button,
    ) -> None:
        self._populate_review(type_dropdown, review_list, status, save_btn)

    def _populate_review(
        self,
        type_dropdown: Gtk.DropDown,
        review_list: Gtk.ListBox,
        status: Gtk.Label,
        save_btn: Gtk.Button,
    ) -> None:
        while row := review_list.get_row_at_index(0):
            review_list.remove(row)
        candidates = cast(
            dict[str, JsonDict],
            self._context.get("candidates", {}),
        )
        ranked = sorted(candidates.values(), key=self._candidate_score, reverse=True)
        analog_type = "stick" if type_dropdown.get_selected() == 1 else "axis"
        needed = 2 if analog_type == "stick" else 1
        if len(ranked) < needed:
            _set_capture_status(status, "Not enough analog movement captured.")
            save_btn.set_sensitive(False)
            return
        if analog_type == "axis" and len(ranked) > 1:
            top = self._candidate_score(ranked[0])
            second = self._candidate_score(ranked[1])
            if top <= 0 or top == second:
                _set_capture_status(status, "Could not choose one axis unambiguously. Try again.")
                save_btn.set_sensitive(False)
                return
        selected = ranked[:needed]
        if any(self._candidate_score(candidate) <= 0 for candidate in selected):
            _set_capture_status(status, "Captured axes did not move far enough.")
            save_btn.set_sensitive(False)
            return

        roles = self._review_roles(selected, analog_type)
        for role, candidate in zip(roles, selected, strict=False):
            review_list.append(self._build_review_row(role, candidate, analog_type))
        review_list.set_visible(True)
        save_btn.set_sensitive(True)
        _set_capture_status(status, "Review the learned values, edit if needed, then save.")

    def _review_roles(
        self,
        selected: list[JsonDict],
        analog_type: str,
    ) -> tuple[str, ...]:
        if analog_type != "stick":
            return ("x",)
        inferred = [self._candidate_stick_role(candidate) for candidate in selected]
        if sorted(role for role in inferred if role is not None) == ["x", "y"]:
            return tuple(cast(str, role) for role in inferred)
        return ("x", "y")

    def _candidate_stick_role(self, candidate: JsonDict) -> str | None:
        evdev_name = str(candidate.get("evdev", "") or "").lower()
        if evdev_name.endswith("x"):
            return "x"
        if evdev_name.endswith("y"):
            return "y"
        try:
            code = int(cast(int, candidate.get("code")))
        except (TypeError, ValueError):
            return None
        if code in {0, 3, 16}:
            return "x"
        if code in {1, 4, 17}:
            return "y"
        return None

    def _build_review_row(
        self,
        role: str,
        candidate: JsonDict,
        analog_type: str,
    ) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)

        title = Gtk.Label(
            label=(f"{candidate.get('evdev')} [{candidate.get('source') or 'default'}]")
        )
        title.set_halign(Gtk.Align.START)
        box.append(title)

        grid = Gtk.Grid()
        grid.set_column_spacing(8)
        grid.set_row_spacing(6)
        role_dropdown: Gtk.DropDown | None = None
        column_offset = 0
        if analog_type == "stick":
            role_label = Gtk.Label(label="Role")
            role_label.add_css_class("caption")
            grid.attach(role_label, 0, 0, 1, 1)
            role_dropdown = Gtk.DropDown.new_from_strings(["X", "Y"])
            assert role_dropdown is not None
            role_dropdown.set_selected(1 if role == "y" else 0)
            grid.attach(role_dropdown, 0, 1, 1, 1)
            column_offset = 1
        minimum = int(candidate["minimum"])
        maximum = int(candidate["maximum"])
        try:
            center_or_rest = int(candidate.get("rest", 0))
        except (TypeError, ValueError):
            center_or_rest = 0
        fields = [
            ("Min", minimum),
            ("Max", maximum),
            (
                "Center" if analog_type == "stick" else "Rest",
                center_or_rest,
            ),
        ]
        spins: list[Gtk.SpinButton] = []
        for column, (label_text, value) in enumerate(fields):
            label = Gtk.Label(label=label_text)
            label.add_css_class("caption")
            grid.attach(label, column + column_offset, 0, 1, 1)
            spin = Gtk.SpinButton()
            spin.set_adjustment(
                Gtk.Adjustment(
                    value=value,
                    lower=-2147483648,
                    upper=2147483647,
                    step_increment=1,
                )
            )
            spin.set_digits(0)
            spin.set_width_chars(8)
            grid.attach(spin, column + column_offset, 1, 1, 1)
            spins.append(spin)
        box.append(grid)

        row._analog_role = role
        row._analog_role_dropdown = role_dropdown
        row._analog_evdev = candidate.get("evdev")
        row._analog_source = candidate.get("source")
        row._analog_stable_path = candidate.get("stable_path")
        row._analog_code = int(candidate["code"])
        row._analog_min_spin = spins[0]
        row._analog_max_spin = spins[1]
        row._analog_rest_spin = spins[2]
        row.set_child(box)
        return row

    def _candidate_score(self, candidate: JsonDict) -> int:
        rest = int(candidate.get("rest", 0))
        observed_minimum = int(candidate.get("observed_minimum", rest))
        observed_maximum = int(candidate.get("observed_maximum", rest))
        return max(abs(observed_maximum - rest), abs(observed_minimum - rest))

    def _on_save_clicked(
        self,
        _button: Gtk.Button,
        dialog: Adw.Dialog,
        type_dropdown: Gtk.DropDown,
        id_entry: Gtk.Entry,
        label_entry: Gtk.Entry,
        review_list: Gtk.ListBox,
        status: Gtk.Label,
    ) -> None:
        analog_type = "stick" if type_dropdown.get_selected() == 1 else "axis"
        analog_id = self._normalize_new_analog_id(id_entry.get_text(), analog_type)
        if self._input_id_exists(analog_id):
            _set_capture_status(status, f"Input id '{analog_id}' already exists.")
            return
        label = label_entry.get_text().strip() or analog_id.replace("_", " ").title()
        axes: list[AnalogAxisDefinition] = []
        source: str | None = None
        stable_path: str | None = None
        roles: list[str] = []
        has_stick_rows = False
        index = 0
        while row := review_list.get_row_at_index(index):
            code = int(row._analog_code)
            row_source = str(row._analog_source or "")
            if self._analog_axis_already_exists(row_source, code):
                _set_capture_status(status, f"Axis {row._analog_evdev} already exists.")
                return
            if source is None and row_source:
                source = row_source
            if stable_path is None and row._analog_stable_path:
                stable_path = str(row._analog_stable_path)
            rest_value = int(row._analog_rest_spin.get_value())
            role_dropdown = row._analog_role_dropdown
            role = (
                "y"
                if isinstance(role_dropdown, Gtk.DropDown) and role_dropdown.get_selected() == 1
                else "x"
                if isinstance(role_dropdown, Gtk.DropDown)
                else str(row._analog_role)
            )
            roles.append(role)
            has_stick_rows = has_stick_rows or isinstance(role_dropdown, Gtk.DropDown)
            axes.append(
                AnalogAxisDefinition(
                    role=role,
                    evdev=str(row._analog_evdev or f"abs_{code}"),
                    evdev_code=code,
                    minimum=int(row._analog_min_spin.get_value()),
                    maximum=int(row._analog_max_spin.get_value()),
                    center=rest_value if analog_type == "stick" else None,
                    rest=rest_value if analog_type == "axis" else None,
                )
            )
            index += 1
        if not axes:
            _set_capture_status(status, "No learned analog axes to save.")
            return
        if analog_type == "stick":
            if sorted(roles) != ["x", "y"]:
                _set_capture_status(status, "Stick needs exactly one X axis and one Y axis.")
                return
            if any(axis.center is None for axis in axes):
                _set_capture_status(status, "Stick axes need a center value.")
                return
        elif has_stick_rows:
            _set_capture_status(status, "Generic axis capture cannot use stick roles.")
            return

        self._on_complete(
            LearnAnalogResult(
                analog=AnalogInputDefinition(
                    id=analog_id,
                    label=label,
                    type=analog_type,
                    source=source,
                    axes=axes,
                ),
                source=source,
                stable_path=stable_path,
            )
        )
        dialog.close()

    def stop_capture(self) -> None:
        self._capturing = False
        self._poll_inflight = False
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = None
        if self._capture_active_hardware_id:
            self._session_request_async(
                {
                    "command": "end_capture",
                    "hardware_id": self._capture_active_hardware_id,
                },
                self._ignore_session_response,
            )
            self._capture_active_hardware_id = None

    def _on_dialog_closed(self, _dialog: Adw.Dialog) -> None:
        self.stop_capture()
        self._context = {}

    def _update_capture_controls(
        self,
        start_btn: Gtk.Button | None,
        unlock_btn: Gtk.Button | None,
        privilege_status: Gtk.Label | None,
    ) -> None:
        if start_btn is None or unlock_btn is None or privilege_status is None:
            return

        unlock_required, recording_unlocked, refresh_owner = self._unlock_state()
        can_capture = not unlock_required or (recording_unlocked and refresh_owner)

        start_btn.set_sensitive(can_capture and not self._capturing)
        if can_capture:
            start_btn.add_css_class("suggested-action")
        else:
            start_btn.remove_css_class("suggested-action")

        if not unlock_required:
            unlock_btn.set_visible(False)
            privilege_status.set_text(
                "Unlock not required. Analog capture reads raw axis events before remapping."
            )
            return

        if can_capture:
            unlock_btn.set_visible(False)
            privilege_status.set_text(
                "Original-input capture is unlocked. Analog capture reads raw axis events before "
                "remapping."
            )
            return

        unlock_btn.set_visible(True)
        label = "Claim" if recording_unlocked else "Unlock"
        unlock_btn.set_child(make_unlock_button_content(label))
        if recording_unlocked:
            unlock_btn.set_tooltip_text(
                "Claim this GUI as the active owner before capturing analog axes."
            )
            privilege_status.set_text(
                "Unlock active in another session. Claim unlock to learn analog inputs."
            )
        else:
            unlock_btn.set_tooltip_text(
                "Authorize raw original-input capture so Keymasq can detect analog axes before "
                "remapping."
            )
            privilege_status.set_text(
                "Original-input capture uses privileged raw events. Unlock to learn analog inputs."
            )

    def _on_unlock_clicked(
        self,
        button: Gtk.Button,
        start_btn: Gtk.Button,
        privilege_status: Gtk.Label,
        status_label: Gtk.Label,
    ) -> None:
        present_unlock = getattr(self.parent_window, "present_unlock_dialog", None)
        if callable(present_unlock):
            present_unlock(
                on_success=lambda: self._on_unlock_success(
                    start_btn,
                    button,
                    privilege_status,
                    status_label,
                )
            )
            return
        _set_capture_status(status_label, "Unlock is only available from the main window.")

    def _on_unlock_success(
        self,
        start_btn: Gtk.Button,
        unlock_btn: Gtk.Button,
        privilege_status: Gtk.Label,
        status_label: Gtk.Label,
    ) -> None:
        _set_capture_status(status_label, "")
        self._update_capture_controls(start_btn, unlock_btn, privilege_status)

    def _unlock_state(self) -> tuple[bool, bool, bool]:
        unlock_required = bool(getattr(self.parent_window, "_recording_unlock_required", True))
        recording_unlocked = bool(getattr(self.parent_window, "_recording_unlocked", False))
        refresh_owner = bool(getattr(self.parent_window, "_recording_refresh_owner", False))
        return unlock_required, recording_unlocked, refresh_owner

    def _on_close_dialog_clicked(self, _button: Gtk.Button, dialog: Adw.Dialog) -> None:
        dialog.close()

    def _ignore_session_response(self, _response: JsonDict | None) -> bool:
        return False

    def _next_analog_id(self, prefix: str) -> str:
        used = {button.id for button in self.hardware_config.buttons}
        used.update(analog.id for analog in self.hardware_config.analog_inputs)
        index = 1
        while f"{prefix}_{index}" in used:
            index += 1
        return f"{prefix}_{index}"

    def _normalize_new_analog_id(self, value: str, analog_type: str) -> str:
        normalized = "".join(
            char.lower() if char.isalnum() else "_" for char in str(value or "")
        ).strip("_")
        return normalized or self._next_analog_id("stick" if analog_type == "stick" else "axis")

    def _input_id_exists(self, input_id: str) -> bool:
        return any(button.id == input_id for button in self.hardware_config.buttons) or any(
            analog.id == input_id for analog in self.hardware_config.analog_inputs
        )

    def _analog_axis_already_exists(self, source: str | None, evdev_code: int) -> bool:
        normalized_source = str(source or "")
        for analog in self.hardware_config.analog_inputs:
            if normalized_source and analog.source and analog.source != normalized_source:
                continue
            for axis in analog.axes:
                existing_code = axis.evdev_code
                if existing_code is None:
                    existing_code = resolve_evdev_code(axis.evdev)
                if existing_code == evdev_code:
                    return True
        return False
