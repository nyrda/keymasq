# pyright: reportAttributeAccessIssue=false
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.actions import MappingAction, ProfileDeactivationPolicy
from keymasq.common.model.core import ActionType
from keymasq.gui.session_client import session_request_async

from .targets import _PROFILE_LIFETIME_PRESETS_ENABLE, _PROFILE_LIFETIME_PRESETS_TOGGLE


class ProfileTabMixin:
    def _restore_profile_lifetime(
        self,
        policy: ProfileDeactivationPolicy | None,
    ) -> None:
        if policy is None:
            self._profile_lifetime_preset = "until_changed"
            return

        self._profile_lifetime_action_count = int(policy.after_actions or 2)
        self._profile_lifetime_timeout_ms = int(policy.timeout_ms or 1500)
        self._profile_custom_trigger_end = bool(policy.on_trigger_end)
        self._profile_custom_action_count = policy.after_actions is not None
        self._profile_custom_timeout = policy.timeout_ms is not None

        simple_trigger = (
            policy.on_trigger_end and policy.after_actions is None and policy.timeout_ms is None
        )
        one_shot = not policy.on_trigger_end and policy.after_actions == 1
        simple_count = (
            not policy.on_trigger_end
            and policy.after_actions is not None
            and policy.timeout_ms is None
        )
        if simple_trigger:
            self._profile_lifetime_preset = "while_trigger_active"
        elif one_shot:
            self._profile_lifetime_preset = "after_one_action"
        elif simple_count:
            self._profile_lifetime_preset = "custom"
        else:
            self._profile_lifetime_preset = "custom"

    def _build_profile_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(12)
        outer.set_margin_end(12)

        title = Gtk.Label(label="Profile Controls")
        title.add_css_class("title-4")
        title.set_halign(Gtk.Align.START)
        outer.append(title)

        subtitle = Gtk.Label(label="Trigger global profile enable/disable/toggle.")
        subtitle.add_css_class("dim-label")
        subtitle.set_wrap(True)
        subtitle.set_halign(Gtk.Align.START)
        outer.append(subtitle)

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_row.set_halign(Gtk.Align.START)

        action_label = Gtk.Label(label="Action")
        action_label.set_size_request(90, -1)
        action_label.set_halign(Gtk.Align.START)
        action_row.append(action_label)

        self._profile_action_dropdown = Gtk.DropDown()
        action_model = Gtk.StringList()
        action_model.append("Enable")
        action_model.append("Toggle")
        action_model.append("Disable")
        self._profile_action_dropdown.set_model(action_model)
        self._profile_action_dropdown.set_selected(
            {"enable": 0, "toggle": 1, "disable": 2}.get(self._selected_profile_action, 0)
        )
        self._profile_action_dropdown.connect("notify::selected", self._on_profile_action_changed)
        action_row.append(self._profile_action_dropdown)
        outer.append(action_row)

        profile_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        profile_row.set_halign(Gtk.Align.START)

        profile_label = Gtk.Label(label="Profile")
        profile_label.set_size_request(90, -1)
        profile_label.set_halign(Gtk.Align.START)
        profile_row.append(profile_label)

        self._profile_name_dropdown = Gtk.DropDown()
        self._profile_name_model = Gtk.StringList()
        self._profile_name_dropdown.set_model(self._profile_name_model)
        self._profile_name_dropdown.set_size_request(360, -1)
        self._profile_name_dropdown.connect("notify::selected", self._on_profile_name_changed)
        profile_row.append(self._profile_name_dropdown)
        outer.append(profile_row)

        self._profile_lifetime_box = self._build_profile_lifetime_controls()
        outer.append(self._profile_lifetime_box)

        self._profile_hint_label = Gtk.Label(label="")
        self._profile_hint_label.add_css_class("dim-label")
        self._profile_hint_label.set_halign(Gtk.Align.START)
        self._profile_hint_label.set_wrap(True)
        outer.append(self._profile_hint_label)

        GLib.idle_add(self._load_profile_overview)
        return outer

    def _load_profile_overview(self) -> bool:
        session_request_async({"command": "list_profiles"}, self._on_profile_overview_loaded)
        return False

    def _on_profile_overview_loaded(self, result: dict | None) -> bool:
        result = result or {}
        self._profile_entries = result.get("profiles", []) if result.get("status") == "ok" else []
        self._populate_profile_names()
        return False

    def _on_profile_action_changed(self, dropdown, _pspec) -> None:
        idx = dropdown.get_selected()
        if idx == 0:
            self._selected_profile_action = "enable"
        elif idx == 2:
            self._selected_profile_action = "disable"
        else:
            self._selected_profile_action = "toggle"
        self._profile_lifetime_preset = self._coerce_profile_lifetime_preset(
            self._profile_lifetime_preset
        )
        self._update_profile_lifetime_visibility()
        self._update_profile_hint()

    def _build_profile_lifetime_controls(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_halign(Gtk.Align.START)
        self._profile_lifetime_box = box

        self._profile_lifetime_title = Gtk.Label(label="Activation")
        self._profile_lifetime_title.set_halign(Gtk.Align.START)
        box.append(self._profile_lifetime_title)

        preset_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        preset_label = Gtk.Label(label="Mode")
        preset_label.set_size_request(90, -1)
        preset_label.set_halign(Gtk.Align.START)
        preset_row.append(preset_label)

        self._profile_lifetime_dropdown = Gtk.DropDown()
        self._profile_lifetime_model = Gtk.StringList()
        self._sync_profile_lifetime_model()
        self._profile_lifetime_dropdown.set_model(self._profile_lifetime_model)
        self._profile_lifetime_dropdown.set_selected(
            self._profile_lifetime_preset_index(self._profile_lifetime_preset)
        )
        self._profile_lifetime_dropdown.connect(
            "notify::selected",
            self._on_profile_lifetime_preset_changed,
        )
        preset_row.append(self._profile_lifetime_dropdown)
        box.append(preset_row)

        self._profile_lifetime_timeout_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        timeout_label = Gtk.Label(label="Timeout")
        timeout_label.set_size_request(90, -1)
        timeout_label.set_halign(Gtk.Align.START)
        self._profile_lifetime_timeout_row.append(timeout_label)
        self._profile_lifetime_timeout_adjustment = Gtk.Adjustment(
            value=self._profile_lifetime_timeout_ms,
            lower=1,
            upper=3_600_000,
            step_increment=50,
        )
        self._profile_lifetime_timeout_spin = Gtk.SpinButton()
        self._profile_lifetime_timeout_spin.set_adjustment(
            self._profile_lifetime_timeout_adjustment
        )
        self._profile_lifetime_timeout_spin.connect(
            "value-changed",
            self._on_profile_lifetime_timeout_changed,
        )
        self._profile_lifetime_timeout_row.append(self._profile_lifetime_timeout_spin)
        timeout_unit = Gtk.Label(label="ms")
        timeout_unit.add_css_class("dim-label")
        self._profile_lifetime_timeout_row.append(timeout_unit)
        box.append(self._profile_lifetime_timeout_row)

        self._profile_lifetime_custom_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
        )

        self._profile_custom_count_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        self._profile_custom_count_row.set_halign(Gtk.Align.START)
        self._profile_custom_count_toggle = Gtk.ToggleButton(label="Action count")
        self._profile_custom_count_toggle.set_size_request(140, -1)
        self._profile_custom_count_toggle.set_active(self._profile_custom_action_count)
        self._profile_custom_count_toggle.connect(
            "toggled",
            self._on_profile_custom_count_toggled,
        )
        self._profile_custom_count_row.append(self._profile_custom_count_toggle)
        self._profile_lifetime_count_spin = Gtk.SpinButton()
        self._profile_lifetime_count_spin.set_size_request(92, -1)
        self._profile_lifetime_count_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._profile_lifetime_action_count,
                lower=1,
                upper=999,
                step_increment=1,
            )
        )
        self._profile_lifetime_count_spin.connect(
            "value-changed",
            self._on_profile_lifetime_count_changed,
        )
        self._profile_custom_count_row.append(self._profile_lifetime_count_spin)
        self._profile_lifetime_custom_box.append(self._profile_custom_count_row)

        self._profile_custom_timeout_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        self._profile_custom_timeout_row.set_halign(Gtk.Align.START)
        self._profile_custom_timeout_toggle = Gtk.ToggleButton(label="Timeout")
        self._profile_custom_timeout_toggle.set_size_request(140, -1)
        self._profile_custom_timeout_toggle.set_active(self._profile_custom_timeout)
        self._profile_custom_timeout_toggle.connect(
            "toggled",
            self._on_profile_custom_timeout_toggled,
        )
        self._profile_custom_timeout_row.append(self._profile_custom_timeout_toggle)
        self._profile_custom_timeout_spin = Gtk.SpinButton()
        self._profile_custom_timeout_spin.set_size_request(92, -1)
        self._profile_custom_timeout_spin.set_adjustment(self._profile_lifetime_timeout_adjustment)
        self._profile_custom_timeout_spin.connect(
            "value-changed",
            self._on_profile_lifetime_timeout_changed,
        )
        self._profile_custom_timeout_row.append(self._profile_custom_timeout_spin)
        self._profile_custom_timeout_unit = Gtk.Label(label="ms")
        self._profile_custom_timeout_unit.add_css_class("dim-label")
        self._profile_custom_timeout_row.append(self._profile_custom_timeout_unit)
        self._profile_lifetime_custom_box.append(self._profile_custom_timeout_row)

        self._profile_custom_trigger_toggle = Gtk.ToggleButton(label="Trigger end")
        self._profile_custom_trigger_toggle.set_halign(Gtk.Align.START)
        self._profile_custom_trigger_toggle.set_size_request(140, -1)
        self._profile_custom_trigger_toggle.set_active(self._profile_custom_trigger_end)
        self._profile_custom_trigger_toggle.connect(
            "toggled",
            self._on_profile_custom_trigger_toggled,
        )
        self._profile_lifetime_custom_box.append(self._profile_custom_trigger_toggle)

        box.append(self._profile_lifetime_custom_box)

        self._profile_lifetime_notice_label = Gtk.Label(
            label="Disable this profile first to use it as a temporary layer."
        )
        self._profile_lifetime_notice_label.add_css_class("dim-label")
        self._profile_lifetime_notice_label.set_halign(Gtk.Align.START)
        self._profile_lifetime_notice_label.set_wrap(True)
        box.append(self._profile_lifetime_notice_label)

        self._update_profile_lifetime_visibility()
        return box

    def _on_profile_lifetime_preset_changed(self, dropdown, _pspec) -> None:
        if self._profile_lifetime_selection_updating:
            return
        if self._profile_lifetime_blocked():
            self._sync_profile_lifetime_dropdown("until_changed")
            self._update_profile_lifetime_visibility()
            self._update_profile_hint()
            return
        idx = int(dropdown.get_selected())
        presets = self._profile_lifetime_presets()
        self._profile_lifetime_preset = (
            presets[idx][0] if 0 <= idx < len(presets) else "until_changed"
        )
        self._update_profile_lifetime_visibility()
        self._update_profile_hint()

    def _on_profile_lifetime_count_changed(self, spin: Gtk.SpinButton) -> None:
        self._profile_lifetime_action_count = int(spin.get_value())

    def _on_profile_lifetime_timeout_changed(self, spin: Gtk.SpinButton) -> None:
        self._profile_lifetime_timeout_ms = int(spin.get_value())

    def _on_profile_custom_trigger_toggled(self, check: Gtk.ToggleButton) -> None:
        self._profile_custom_trigger_end = bool(check.get_active())

    def _on_profile_custom_count_toggled(self, check: Gtk.ToggleButton) -> None:
        self._profile_custom_action_count = bool(check.get_active())
        self._update_profile_lifetime_visibility()

    def _on_profile_custom_timeout_toggled(self, check: Gtk.ToggleButton) -> None:
        self._profile_custom_timeout = bool(check.get_active())
        self._update_profile_lifetime_visibility()

    def _profile_lifetime_presets(self) -> tuple[tuple[str, str], ...]:
        if self._selected_profile_action == "toggle":
            return _PROFILE_LIFETIME_PRESETS_TOGGLE
        return _PROFILE_LIFETIME_PRESETS_ENABLE

    def _coerce_profile_lifetime_preset(self, preset: str) -> str:
        keys = {key for key, _label in self._profile_lifetime_presets()}
        return preset if preset in keys else "until_changed"

    def _profile_lifetime_preset_index(self, preset: str) -> int:
        preset = self._coerce_profile_lifetime_preset(preset)
        for idx, (key, _label) in enumerate(self._profile_lifetime_presets()):
            if key == preset:
                return idx
        return 0

    def _sync_profile_lifetime_model(self) -> None:
        if not hasattr(self, "_profile_lifetime_model"):
            return
        presets = self._profile_lifetime_presets()
        keys = [key for key, _label in presets]
        if self._profile_lifetime_model_keys == keys:
            return
        self._profile_lifetime_selection_updating = True
        try:
            while self._profile_lifetime_model.get_n_items() > 0:
                self._profile_lifetime_model.remove(0)
            for _key, label in presets:
                self._profile_lifetime_model.append(label)
            self._profile_lifetime_model_keys = keys
        finally:
            self._profile_lifetime_selection_updating = False

    def _sync_profile_lifetime_dropdown(self, preset: str) -> None:
        if not hasattr(self, "_profile_lifetime_dropdown"):
            return
        self._sync_profile_lifetime_model()
        preset = self._coerce_profile_lifetime_preset(preset)
        idx = self._profile_lifetime_preset_index(preset)
        if int(self._profile_lifetime_dropdown.get_selected()) == idx:
            return
        self._profile_lifetime_selection_updating = True
        try:
            self._profile_lifetime_dropdown.set_selected(idx)
        finally:
            self._profile_lifetime_selection_updating = False

    def _selected_profile_entry(self) -> dict | None:
        for profile in self._profile_entries:
            if str(profile.get("name", "") or "") == self._selected_profile_name:
                return profile
        return None

    def _selected_profile_enabled(self) -> bool:
        profile = self._selected_profile_entry()
        return bool(profile and bool(profile.get("enabled", True)))

    def _profile_lifetime_available(self) -> bool:
        return self._selected_profile_action in {"enable", "toggle"}

    def _profile_lifetime_blocked(self) -> bool:
        return self._profile_lifetime_available() and self._selected_profile_enabled()

    def _update_profile_lifetime_visibility(self) -> None:
        if not hasattr(self, "_profile_lifetime_box"):
            return
        lifetime_available = self._profile_lifetime_available()
        blocked = self._profile_lifetime_blocked()
        preset = (
            "until_changed"
            if blocked
            else self._coerce_profile_lifetime_preset(self._profile_lifetime_preset)
        )
        if not blocked:
            self._profile_lifetime_preset = preset
        is_custom = preset == "custom"
        is_one_shot = preset == "after_one_action"
        allow_trigger_end = self._selected_profile_action == "enable"
        if not allow_trigger_end and self._profile_custom_trigger_end:
            self._profile_custom_trigger_end = False
            self._profile_custom_trigger_toggle.set_active(False)
        self._sync_profile_lifetime_dropdown(preset)
        self._profile_lifetime_box.set_visible(lifetime_available)
        self._profile_lifetime_title.set_label(
            "When toggled on" if self._selected_profile_action == "toggle" else "Activation"
        )
        self._profile_lifetime_dropdown.set_sensitive(not blocked)
        self._profile_lifetime_timeout_row.set_visible(False)
        self._profile_lifetime_custom_box.set_visible(is_custom or is_one_shot)
        self._profile_custom_count_row.set_visible(is_custom)
        self._profile_custom_timeout_row.set_visible(is_custom or is_one_shot)
        self._profile_custom_trigger_toggle.set_visible(is_custom and allow_trigger_end)
        self._profile_lifetime_count_spin.set_sensitive(self._profile_custom_action_count)
        self._profile_custom_timeout_spin.set_visible(self._profile_custom_timeout)
        self._profile_custom_timeout_spin.set_sensitive(self._profile_custom_timeout)
        self._profile_custom_timeout_unit.set_visible(self._profile_custom_timeout)
        self._profile_lifetime_notice_label.set_visible(blocked)

    def _populate_profile_names(self) -> None:
        profile_name_items: list[str] = []
        profile_name_labels: list[str] = []
        for profile in self._profile_entries:
            name = str(profile.get("name", "") or "")
            if not name:
                continue
            profile_name_items.append(name)
            enabled = bool(profile.get("enabled", True))
            marker = "" if enabled else " [disabled]"
            profile_name_labels.append(f"{name}{marker}")

        self._profile_name_populating = True
        try:
            self._profile_name_items = profile_name_items
            while self._profile_name_model.get_n_items() > 0:
                self._profile_name_model.remove(0)
            for label in profile_name_labels:
                self._profile_name_model.append(label)
        finally:
            self._profile_name_populating = False

        if not self._profile_name_items:
            self._selected_profile_name = ""
            self._update_profile_hint()
            self._on_tab_changed(self.stack, None)
            return

        selected_index = 0
        if self._selected_profile_name in self._profile_name_items:
            selected_index = self._profile_name_items.index(self._selected_profile_name)
        else:
            self._selected_profile_name = self._profile_name_items[0]

        self._profile_name_dropdown.set_selected(selected_index)
        self._on_profile_name_changed(self._profile_name_dropdown, None)

    def _on_profile_name_changed(self, dropdown, _pspec) -> None:
        if self._profile_name_populating or not self._profile_name_items:
            return
        idx = int(dropdown.get_selected())
        if idx < 0 or idx >= len(self._profile_name_items):
            self._selected_profile_name = ""
        else:
            self._selected_profile_name = self._profile_name_items[idx]
        self._update_profile_lifetime_visibility()
        self._update_profile_hint()
        self._on_tab_changed(self.stack, None)

    def _update_profile_hint(self) -> None:
        if not self._selected_profile_name:
            self._profile_hint_label.set_label("Select a profile to map this action.")
            return

        verb = {
            "toggle": "Toggle",
            "enable": "Enable",
            "disable": "Disable",
        }.get(self._selected_profile_action, "Toggle")
        self._profile_hint_label.set_label(f"{verb} profile '{self._selected_profile_name}'.")

    def _on_profile_map_clicked(self, btn) -> None:
        if not self._selected_profile_name:
            return

        action_type = ActionType.PROFILE_TOGGLE
        if self._selected_profile_action == "enable":
            action_type = ActionType.PROFILE_ENABLE
        elif self._selected_profile_action == "disable":
            action_type = ActionType.PROFILE_DISABLE

        self._warn_and_clear_unsupported_rapidfire(action_type)
        action = MappingAction(
            action_type=action_type,
            profile_name=self._selected_profile_name,
            profile_deactivation=self._profile_deactivation_policy(action_type),
        )
        self._emit_selected_action(action)

    def _profile_deactivation_policy(
        self,
        action_type: ActionType,
    ) -> ProfileDeactivationPolicy | None:
        if action_type not in (ActionType.PROFILE_ENABLE, ActionType.PROFILE_TOGGLE):
            return None
        if self._profile_lifetime_blocked():
            return None
        preset = self._coerce_profile_lifetime_preset(self._profile_lifetime_preset)
        if preset == "while_trigger_active":
            if action_type == ActionType.PROFILE_ENABLE:
                return ProfileDeactivationPolicy(on_trigger_end=True)
            return None
        if preset == "after_one_action":
            timeout_ms = (
                max(1, int(self._profile_lifetime_timeout_ms))
                if self._profile_custom_timeout
                else None
            )
            return ProfileDeactivationPolicy(after_actions=1, timeout_ms=timeout_ms)
        if preset != "custom":
            return None

        after_actions = (
            max(1, int(self._profile_lifetime_action_count))
            if self._profile_custom_action_count
            else None
        )
        timeout_ms = (
            max(1, int(self._profile_lifetime_timeout_ms)) if self._profile_custom_timeout else None
        )
        policy = ProfileDeactivationPolicy(
            on_trigger_end=(
                bool(self._profile_custom_trigger_end)
                if action_type == ActionType.PROFILE_ENABLE
                else False
            ),
            after_actions=after_actions,
            timeout_ms=timeout_ms,
        )
        return policy if policy.has_condition else None
