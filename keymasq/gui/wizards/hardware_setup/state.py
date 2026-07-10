from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .types import DetectedDevice, DetectedInterface

WizardAction = Literal[
    "none",
    "emit_evdev",
    "show_describe",
    "save_keyboard",
    "save_mouse",
    "save_mouse_keyboard",
    "save_gamepad",
    "save_custom",
]


@dataclass
class WizardNavigation:
    select_evdev_only: bool = False
    page: Literal["select", "describe"] = "select"

    def next_action(
        self,
        *,
        has_selection: bool,
        discovery_inflight: bool,
        configure_mode: str,
    ) -> WizardAction:
        if self.page == "select":
            if not has_selection or discovery_inflight:
                return "none"
            if self.select_evdev_only:
                return "emit_evdev"
            self.page = "describe"
            return "show_describe"
        if configure_mode == "keyboard":
            return "save_keyboard"
        if configure_mode == "gamepad":
            return "save_gamepad"
        if configure_mode == "mouse_keyboard":
            return "save_mouse_keyboard"
        if configure_mode == "custom":
            return "save_custom"
        return "save_mouse"

    def back(self) -> bool:
        if self.page != "describe":
            return False
        self.page = "select"
        return True


@dataclass
class DiscoverySelection:
    detected_devices: dict[str, DetectedDevice] = field(default_factory=dict)
    selected_device: DetectedDevice | None = None
    discovered_interfaces: dict[str, DetectedInterface] = field(default_factory=dict)
    detecting: bool = False
    discovering: bool = False
    request_id: int = 0
    show_raw: bool = False

    def begin_detection(self) -> bool:
        if self.detecting:
            return False
        self.detecting = True
        return True

    def finish_detection(self) -> None:
        self.detecting = False

    def clear_selection(self) -> None:
        self.selected_device = None
        self.discovered_interfaces = {}
        self.request_id += 1
        self.discovering = False

    def select(self, device: DetectedDevice) -> int:
        self.selected_device = device
        self.discovered_interfaces = {}
        self.request_id += 1
        self.discovering = True
        return self.request_id

    def accepts(self, request_id: int, selected_key: str, current_key: str) -> bool:
        return (
            request_id == self.request_id
            and self.selected_device is not None
            and selected_key == current_key
        )

    def finish_discovery(self, request_id: int) -> None:
        if request_id == self.request_id:
            self.discovering = False


@dataclass
class TemplateSelection:
    current: str = ""
    values: list[str] = field(default_factory=lambda: ["mouse"])

    def refresh(self, roles: set[str], *, show_raw: bool) -> list[str]:
        values: list[str] = []
        if "gamepad" in roles:
            values.append("gamepad")
        has_mouse = bool({"mouse", "pointstick"} & roles)
        has_keyboard = "keyboard" in roles
        if has_mouse and has_keyboard:
            values.append("mouse_keyboard")
        if has_mouse:
            values.append("mouse")
        if has_keyboard:
            values.append("keyboard")
        if not values:
            values.append("custom" if show_raw else "mouse")
        self.values = values
        self.current = self.preferred()
        return values

    def preferred(self) -> str:
        if self.current in self.values:
            return self.current
        for preferred in ("gamepad", "mouse_keyboard", "mouse", "keyboard", "custom"):
            if preferred in self.values:
                return preferred
        return self.values[0]

    def select(self, index: int) -> bool:
        if index < 0 or index >= len(self.values):
            return False
        self.current = self.values[index]
        return True
