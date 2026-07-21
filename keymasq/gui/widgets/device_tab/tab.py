"""GTK controller composing the focused device-tab workflows."""

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.hardware import HardwareConfig
from keymasq.gui.session_client import JsonDict, session_request_async
from keymasq.gui.widgets.device_tab.capture import CaptureMixin
from keymasq.gui.widgets.device_tab.commit import DeferredCommitState, SelectorCommitMixin
from keymasq.gui.widgets.device_tab.hardware import HardwareSettingsMixin
from keymasq.gui.widgets.device_tab.hardware_settings_dialog import HardwareSettingsDialog
from keymasq.gui.widgets.device_tab.inputs import InputInventoryMixin
from keymasq.gui.widgets.device_tab.inventory import InventoryMixin
from keymasq.gui.widgets.device_tab.mapping import MappingMixin
from keymasq.gui.widgets.device_tab.presentation import ProfilePresentationMixin
from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog
from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab
from keymasq.session.hardware import HardwareManager
from keymasq.session.profile.manager import ProfileManager

SessionCallback = Callable[[JsonDict | None], bool]
SELECTOR_COMMIT_AFTER_CLOSE_DELAY_MS = 500


class DeviceTab(
    InventoryMixin,
    InputInventoryMixin,
    MappingMixin,
    CaptureMixin,
    SelectorCommitMixin,
    ProfilePresentationMixin,
    HardwareSettingsMixin,
    ProfileManagedTab,
):
    """Coordinates one hardware device's profile mappings and inventory UI."""

    def __init__(
        self,
        device: HardwareConfig,
        profile_manager: ProfileManager | None,
        hardware_manager: HardwareManager | None = None,
        main_window=None,
        demo_mode: bool = False,
        compositor_capabilities: list[str] | None = None,
    ) -> None:
        self.device = device
        self.hardware_manager = hardware_manager
        self._device_runtime_status: JsonDict = {}
        super().__init__(
            profile_manager=profile_manager,
            main_window=main_window,
            demo_mode=demo_mode,
            compositor_capabilities=compositor_capabilities,
        )
        self._hardware_settings_dialog: HardwareSettingsDialog | None = None
        self._button_widgets: dict[str, Gtk.Button] = {}
        self._user_interacting = False
        self._keyboard_layout_mode = False
        self._highlight_timeout_ids: list[int] = []
        self._commit_state = DeferredCommitState()
        self.connect("destroy", self._on_device_tab_destroy)
        self._setup_header()
        self._setup_profile_selector()
        self._setup_button_grid()
        self.refresh_profiles()

    def apply_active_profile_response(self, data: dict | None) -> None:
        self._device_runtime_status = self._device_runtime_status_from_response(data or {})
        super().apply_active_profile_response(data)
        self._update_device_status_pill()
        self._refresh_hardware_settings_runtime_metadata()

    def _request_session_async(
        self,
        payload: JsonDict,
        callback: SessionCallback,
        timeout: float | None = None,
    ) -> object:
        if timeout is None:
            return session_request_async(payload, callback)
        return session_request_async(payload, callback, timeout=timeout)

    def _create_key_selector_dialog(
        self,
        parent: Gtk.Widget,
        title: str,
        current_action: MappingAction | None,
        **kwargs: Any,
    ) -> KeySelectorDialog:
        return KeySelectorDialog(parent, title, current_action, **kwargs)

    def _schedule_selector_commit(self, callback: Callable[[], bool]) -> int:
        return int(GLib.timeout_add(SELECTOR_COMMIT_AFTER_CLOSE_DELAY_MS, callback))

    def _remove_selector_commit_source(self, source_id: int) -> None:
        GLib.source_remove(source_id)

    def _on_close_dialog_clicked(self, _button: Gtk.Button, dialog: Adw.Dialog) -> None:
        dialog.close()

    def _ignore_session_response(self, _response: JsonDict | None) -> bool:
        return False

    def _reload_ui(self) -> None:
        selected_name = self._selected_profile.config.name if self._selected_profile else None
        selected_name = self._window_selected_profile_name() or selected_name
        assert self.profile_manager is not None
        self.profiles = self.profile_manager.list_profiles()
        while child := self.get_first_child():
            self.remove(child)
        self._button_widgets = {}
        self._setup_header()
        self._setup_profile_selector()
        self._setup_button_grid()
        if selected_name:
            for index, name in enumerate(self._profile_names):
                if name == selected_name:
                    self.profile_dropdown.set_selected(index)
                    break
        self._apply_profile_selection()
