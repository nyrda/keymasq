# ruff: noqa: F403, F405, I001
from tests.gui.support import *

class TestHardwareSetupDialog:
    def test_hardware_setup_uses_inline_adw_dialog(self, monkeypatch):
        gi.require_version("Adw", "1")
        gi.require_version("Gtk", "4.0")
        from gi.repository import Adw, Gtk

        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())

        assert isinstance(dialog, Adw.Dialog)
        assert not isinstance(dialog, Gtk.Window)

    def test_hardware_setup_escape_closes_dialog(self, monkeypatch):
        gi.require_version("Gdk", "4.0")
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gdk, Gtk

        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())
        closed: list[bool] = []
        dialog.close = lambda: closed.append(True)  # type: ignore[method-assign]

        handled = dialog._on_key_pressed(
            Gtk.EventControllerKey.new(),
            Gdk.KEY_Escape,
            0,
            Gdk.ModifierType(0),
        )

        assert handled is True
        assert closed == [True]

    def test_hardware_setup_close_stops_active_capture(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())
        stopped: list[bool] = []
        dialog._capturing = True
        dialog._stop_capture = lambda: stopped.append(True)  # type: ignore[method-assign]

        dialog._on_closed()

        assert stopped == [True]

    def test_uinput_identity_groups_same_model_over_unstable_event_path(self):
        from keymasq.gui.wizards.hardware_setup import _logical_hardware_identity_key

        first_key = _logical_hardware_identity_key(
            model_id="1234:1001",
            device_types=["gamepad"],
            stable_path="/dev/input/event30",
            phys="py-evdev-uinput",
        )
        second_key = _logical_hardware_identity_key(
            model_id="1234:1001",
            device_types=["keyboard"],
            stable_path="/dev/input/event7",
            phys="py-evdev-uinput",
        )
        other_model_key = _logical_hardware_identity_key(
            model_id="046d:c24f",
            device_types=["gamepad"],
            stable_path="/dev/input/event29",
            phys="py-evdev-uinput",
        )

        assert first_key == second_key
        assert first_key == "uinput-model:1234:1001"
        assert other_model_key == "uinput-model:046d:c24f"

    def test_interface_id_for_config_uses_type_without_by_id(self):
        from keymasq.gui.wizards.hardware_setup import _interface_id_for_config

        used_ids: set[str] = set()

        first = _interface_id_for_config(
            {
                "stable_path": "/dev/input/event7",
                "config_path": "keymasq:2dc8:3106",
                "device_types": ["gamepad"],
            },
            used_ids,
        )
        second = _interface_id_for_config(
            {
                "stable_path": "/dev/input/event8",
                "config_path": "keymasq:2dc8:3106",
                "device_types": ["gamepad"],
            },
            used_ids,
        )

        assert first == "gamepad"
        assert second == "gamepad_2"

    def test_interface_id_for_config_uses_by_id_suffix(self):
        from keymasq.gui.wizards.hardware_setup import _interface_id_for_config

        iface_id = _interface_id_for_config(
            {
                "stable_path": "/dev/input/by-id/usb-Test-if02-event-joystick",
                "config_path": "/dev/input/by-id/usb-Test-if02-event-joystick",
                "device_types": ["gamepad"],
            },
            set(),
        )

        assert iface_id == "if02_joystick"

    def test_interface_id_for_config_prefers_by_id_config_path(self):
        from keymasq.gui.wizards.hardware_setup import _interface_id_for_config

        iface_id = _interface_id_for_config(
            {
                "stable_path": "/dev/input/event20",
                "config_path": "/dev/input/by-id/usb-Test-if03-event-kbd",
                "device_types": ["keyboard"],
            },
            set(),
        )

        assert iface_id == "if03_kbd"

    def test_raw_evdev_mode_requests_other_devices_and_disables_grouping(
        self, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.wizards import hardware_setup as hardware_setup_mod
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        requests: list[dict] = []

        def fake_session_request(payload, timeout=3.0):
            requests.append(dict(payload))
            return {
                "status": "ok",
                "devices": [
                    {
                        "path": "/dev/input/event20",
                        "stable_path": "/dev/input/event20",
                        "name": "Virtgaming Generic Joystick",
                        "phys": "py-evdev-uinput",
                        "vendor_id": "1234",
                        "product_id": "1002",
                        "device_type": "other",
                        "device_types": ["other"],
                    },
                    {
                        "path": "/dev/input/event21",
                        "stable_path": "/dev/input/event21",
                        "name": "Virtgaming Generic Joystick Keyboard",
                        "phys": "py-evdev-uinput",
                        "vendor_id": "1234",
                        "product_id": "1002",
                        "device_type": "keyboard",
                        "device_types": ["keyboard"],
                    },
                ],
            }

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
        monkeypatch.setattr(hardware_setup_mod, "session_request", fake_session_request)

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace(get_hardware=lambda _id: None))
        dialog._show_raw_evdev_devices = True
        detected_devices: dict[str, dict] = {}

        assert dialog._detect_devices_via_session(detected_devices) is True

        assert requests == [
            {"command": "list_devices_for_recording", "include_other": True}
        ]
        assert set(detected_devices) == {"raw:/dev/input/event20", "raw:/dev/input/event21"}
        assert detected_devices["raw:/dev/input/event20"]["paths"] == ["/dev/input/event20"]
        assert detected_devices["raw:/dev/input/event21"]["paths"] == ["/dev/input/event21"]

    def test_raw_evdev_toggle_is_disabled_during_detection(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.wizards import hardware_setup as hardware_setup_mod
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        original_detect = HardwareSetupDialog._detect_devices
        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())
        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", original_detect)

        scheduled: list[tuple[object, object, object]] = []
        monkeypatch.setattr(
            hardware_setup_mod,
            "run_gui_task",
            lambda worker, callback, on_done=None: scheduled.append(
                (worker, callback, on_done)
            ),
        )

        assert dialog.raw_evdev_check.get_sensitive() is True

        dialog._detect_devices()

        assert dialog.raw_evdev_check.get_sensitive() is False
        assert len(scheduled) == 1

        dialog._on_detected_devices_done()

        assert dialog.raw_evdev_check.get_sensitive() is True

    def test_raw_evdev_mode_keeps_configured_event_nodes_visible(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.wizards import hardware_setup as hardware_setup_mod
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
        monkeypatch.setattr(
            hardware_setup_mod,
            "session_request",
            lambda _payload, timeout=3.0: {
                "status": "ok",
                "devices": [
                    {
                        "path": "/dev/input/event20",
                        "stable_path": "/dev/input/event20",
                        "name": "Virtgaming Generic Joystick",
                        "phys": "py-evdev-uinput",
                        "vendor_id": "1234",
                        "product_id": "1002",
                        "device_type": "other",
                        "device_types": ["other"],
                    },
                    {
                        "path": "/dev/input/event21",
                        "stable_path": "/dev/input/event21",
                        "name": "Virtgaming Generic Joystick Keyboard",
                        "phys": "py-evdev-uinput",
                        "vendor_id": "1234",
                        "product_id": "1002",
                        "device_type": "keyboard",
                        "device_types": ["keyboard"],
                    },
                ],
            },
        )

        configured = HardwareConfig(
            vendor_id="1234",
            product_id="1002",
            name="Virtgaming Generic Joystick",
            evdev_devices=[
                EvdevDevice(path="/dev/input/event20", device_type=DeviceType.OTHER)
            ],
            buttons=[],
        )
        hardware_manager = SimpleNamespace(list_hardware=lambda: [configured])
        dialog = HardwareSetupDialog(Gtk.Window(), hardware_manager)
        dialog._show_raw_evdev_devices = True
        detected_devices: dict[str, dict] = {}

        assert dialog._detect_devices_via_session(detected_devices) is True
        assert set(detected_devices) == {
            "1234:1002#/dev/input/event20",
            "raw:/dev/input/event21",
        }
        assert detected_devices["1234:1002#/dev/input/event20"]["paths"] == [
            "/dev/input/event20"
        ]
        assert detected_devices["1234:1002#/dev/input/event20"]["interfaces"][0][
            "configured_hardware_id"
        ] == "1234:1002"
        assert detected_devices["raw:/dev/input/event21"]["paths"] == ["/dev/input/event21"]

    def test_raw_evdev_mode_keeps_grabbed_rows_without_consuming_duplicate_id(
        self, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.wizards import hardware_setup as hardware_setup_mod
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
        monkeypatch.setattr(
            hardware_setup_mod,
            "session_request",
            lambda _payload, timeout=3.0: {
                "status": "ok",
                "devices": [
                    {
                        "path": "/dev/input/event20",
                        "stable_path": "/dev/input/event20",
                        "name": "Xbox 360 Wireless Receiver",
                        "phys": "usb-0000:0e:00.3-1.2/input0",
                        "vendor_id": "045e",
                        "product_id": "02a1",
                        "device_type": "gamepad",
                        "device_types": ["gamepad"],
                        "grabbed_by_keymasq": True,
                        "source_hardware_id": "045e:02a1",
                        "source_interface_id": "gamepad",
                    },
                    {
                        "path": "/dev/input/event21",
                        "stable_path": "/dev/input/event21",
                        "name": "Xbox 360 Wireless Receiver",
                        "phys": "usb-0000:0e:00.3-1.3/input0",
                        "vendor_id": "045e",
                        "product_id": "02a1",
                        "device_type": "gamepad",
                        "device_types": ["gamepad"],
                    },
                ],
            },
        )

        hardware_manager = SimpleNamespace(list_hardware_ids=lambda: ["045e:02a1"])
        dialog = HardwareSetupDialog(Gtk.Window(), hardware_manager)
        dialog._show_raw_evdev_devices = True
        detected_devices: dict[str, dict] = {}

        assert dialog._detect_devices_via_session(detected_devices) is True

        assert set(detected_devices) == {
            "045e:02a1#/dev/input/event20",
            "raw:/dev/input/event21",
        }
        assert detected_devices["045e:02a1#/dev/input/event20"]["hardware_id"] == "045e:02a1"
        assert detected_devices["045e:02a1#/dev/input/event20"]["interfaces"][0][
            "grabbed_by_keymasq"
        ] is True
        assert detected_devices["raw:/dev/input/event21"]["paths"] == ["/dev/input/event21"]

    def test_raw_unknown_device_uses_custom_empty_profile_mode(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import DeviceType
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        class _HardwareManager:
            def __init__(self) -> None:
                self.saved = []

            def save_hardware(self, config) -> None:
                self.saved.append(config)

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        hardware_manager = _HardwareManager()
        dialog = HardwareSetupDialog(Gtk.Window(), hardware_manager)
        dialog._show_raw_evdev_devices = True
        dialog.selected_device = {
            "vendor_id": "1234",
            "product_id": "1002",
            "name": "Raw Device",
            "interfaces": [
                {
                    "device_type": DeviceType.OTHER,
                    "device_types": ["other"],
                }
            ],
        }
        dialog.discovered_interfaces = {
            "event20": {
                "id": "event20",
                "stable_path": "/dev/input/event20",
                "path": "/dev/input/event20",
                "name": "Raw Device",
                "device_type": DeviceType.OTHER,
                "device_types": ["other"],
            }
        }
        emitted = []
        dialog.emit = lambda signal, config: emitted.append((signal, config))
        dialog.close = lambda: None

        dialog._refresh_configure_modes()
        dialog._save_custom_config()

        assert dialog._configure_mode_values == ["custom"]
        assert dialog._configure_mode == "custom"
        assert dialog.describe_subtitle.get_label() == "Create an empty profile for this raw device"
        saved = hardware_manager.saved[0]
        assert saved.evdev_devices[0].device_type == DeviceType.OTHER
        assert saved.buttons == []
        assert emitted == [("device-created", saved)]

    def test_raw_evdev_discovery_uses_logical_config_path_without_by_id(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import DeviceType
        from keymasq.gui.wizards import hardware_setup as hardware_setup_mod
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
        monkeypatch.setattr(
            HardwareSetupDialog,
            "_read_interface_capabilities",
            lambda self, _path: ([], {}),
        )

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())
        dialog._show_raw_evdev_devices = True
        dialog.selected_device = {
            "vendor_id": "1234",
            "product_id": "1002",
            "interfaces": [
                {
                    "path": "/dev/input/event20",
                    "stable_path": "/dev/input/event20",
                    "name": "Raw Device",
                    "device_type": DeviceType.OTHER,
                    "device_types": ["other"],
                }
            ],
        }

        dialog._discover_interfaces()

        raw_iface = next(iter(dialog.discovered_interfaces.values()))
        assert raw_iface["config_path"] == "keymasq:1234:1002"

        dialog.selected_device = {
            "vendor_id": "1234",
            "product_id": "1002",
            "interfaces": [],
        }
        monkeypatch.setattr(
            hardware_setup_mod,
            "find_all_interfaces",
            lambda _vid, _pid: [
                {
                    "path": "/dev/input/event21",
                    "stable_path": "/dev/input/event21",
                    "name": "Raw Device Keyboard",
                }
            ],
        )

        dialog._discover_interfaces()

        fallback_iface = next(iter(dialog.discovered_interfaces.values()))
        assert fallback_iface["config_path"] == "keymasq:1234:1002"

    def test_normal_rows_show_interface_expander_and_raw_rows_use_summary(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())

        dialog._show_raw_evdev_devices = False
        assert dialog._should_show_interface_expander([{}]) is True
        assert dialog._should_show_interface_expander([{}, {}]) is True

        dialog._show_raw_evdev_devices = True
        assert dialog._should_show_interface_expander([{}]) is False
        assert dialog._should_show_interface_expander([{}, {}]) is False

    def test_selecting_in_use_raw_row_disables_next(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())
        dialog._show_raw_evdev_devices = True
        dialog._discover_interfaces = lambda: None  # type: ignore[method-assign]
        dialog._refresh_configure_modes = lambda: None  # type: ignore[method-assign]
        dialog.detected_devices = {
            "045e:02a1#/dev/input/event20": {
                "vendor_id": "045e",
                "product_id": "02a1",
                "name": "Xbox 360 Wireless Receiver",
                "interfaces": [
                    {
                        "path": "/dev/input/event20",
                        "grabbed_by_keymasq": True,
                        "source_hardware_id": "045e:02a1",
                        "source_interface_id": "gamepad",
                    }
                ],
            }
        }
        row = Gtk.ListBoxRow()
        row.hardware_id = "045e:02a1#/dev/input/event20"
        dialog.device_list.append(row)

        dialog._on_device_selected(None, row)

        assert dialog.selected_device is dialog.detected_devices[row.hardware_id]
        assert dialog.next_btn.get_sensitive() is False
        assert dialog._device_in_use_summary(dialog.selected_device) == (
            "In use by 045e:02a1 (gamepad)"
        )

    def test_device_in_use_summary_ignores_non_dict_interfaces(self):
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        summary = HardwareSetupDialog._device_in_use_summary(
            {
                "interfaces": [
                    "invalid",
                    {"configured_hardware_id": "045e:02a1"},
                ]
            },
        )

        assert summary == "Configured as 045e:02a1"

    def test_selecting_row_without_expander_still_enables_next(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())
        dialog.detected_devices = {
            "1234:1002": {
                "vendor_id": "1234",
                "product_id": "1002",
                "hardware_id": "1234:1002",
                "interfaces": [],
            }
        }
        dialog._discover_interfaces = lambda: None  # type: ignore[method-assign]
        dialog._refresh_configure_modes = lambda: None  # type: ignore[method-assign]
        row = Gtk.ListBoxRow()
        row.hardware_id = "1234:1002"

        dialog._on_device_selected(dialog.device_list, row)

        assert dialog.selected_device is dialog.detected_devices["1234:1002"]
        assert dialog.next_btn.get_sensitive() is True

    def test_hardware_setup_close_without_active_capture_does_not_stop_capture(
        self, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())
        stopped: list[bool] = []
        dialog._capture_hardware_id = "1234:1002"
        dialog._stop_capture = lambda: stopped.append(True)  # type: ignore[method-assign]

        dialog._on_closed()

        assert stopped == []

    def test_refresh_configure_modes_offers_gamepad_first(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())
        dialog.selected_device = {
            "interfaces": [
                {
                    "device_type": "gamepad",
                    "device_types": ["gamepad"],
                },
                {
                    "device_type": "keyboard",
                    "device_types": ["keyboard"],
                },
            ]
        }

        dialog._refresh_configure_modes()

        assert dialog._configure_mode_values == ["gamepad", "keyboard"]
        assert dialog._configure_mode == "gamepad"
        assert dialog.describe_subtitle.get_label() == "Review the detected controller controls"

    def test_refresh_configure_modes_prefers_mouse_keyboard_template(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())
        dialog.selected_device = {
            "interfaces": [
                {
                    "device_type": "mouse",
                    "device_types": ["mouse"],
                },
                {
                    "device_type": "keyboard",
                    "device_types": ["keyboard"],
                },
            ]
        }

        dialog._refresh_configure_modes()

        assert dialog._configure_mode_values == ["mouse_keyboard", "mouse", "keyboard"]
        assert dialog._configure_mode == "mouse_keyboard"
        assert (
            dialog.describe_subtitle.get_label()
            == "Create a standard keyboard and mouse profile"
        )

    def test_selected_config_id_only_stores_numbered_ids(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())

        assert (
            dialog._selected_config_id(
                {
                    "vendor_id": "045e",
                    "product_id": "02a1",
                    "hardware_id": "045e:02a1",
                }
            )
            is None
        )
        assert (
            dialog._selected_config_id(
                {
                    "vendor_id": "045e",
                    "product_id": "02a1",
                    "hardware_id": "045e:02a1@2",
                }
            )
            == "045e:02a1@2"
        )

    def test_raw_selected_config_id_allocates_lowest_free_id(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        dialog = HardwareSetupDialog(
            Gtk.Window(),
            SimpleNamespace(list_hardware_ids=lambda: ["045e:02a1"]),
        )
        dialog._show_raw_evdev_devices = True

        assert (
            dialog._selected_config_id(
                {
                    "vendor_id": "045e",
                    "product_id": "02a1",
                    "hardware_id": "045e:02a1",
                    "interfaces": [{"path": "/dev/input/event28"}],
                }
            )
            == "045e:02a1@2"
        )

    def test_detect_devices_via_session_skips_keymasq_virtual_uinput_devices(
        self, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import DeviceType
        from keymasq.gui.wizards import hardware_setup as hardware_setup_mod
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
        session_devices = [
            {
                "path": "/dev/input/event22",
                "name": "keymasq-gamepad",
                "phys": "py-evdev-uinput",
                "vendor_id": "045e",
                "product_id": "028e",
                "device_type": "gamepad",
                "device_types": ["gamepad"],
            },
            {
                "path": "/dev/input/event23",
                "name": "Xbox 360 Controller",
                "phys": "py-evdev-uinput",
                "vendor_id": "045e",
                "product_id": "028e",
                "device_type": "gamepad",
                "device_types": ["gamepad"],
                "recording_kind": "keymasq_output",
            },
            {
                "path": "/dev/input/event24",
                "name": "Logitech G920 Driving Force Racing Wheel for Xbox One",
                "phys": "py-evdev-uinput",
                "vendor_id": "046d",
                "product_id": "c262",
                "device_type": "gamepad",
                "device_types": ["gamepad"],
                "recording_kind": "keymasq_passthrough",
                "source_hardware_id": "046d:c262",
            },
            {
                "path": "/dev/input/event10",
                "name": "Real USB Mouse",
                "phys": "usb-0000:00:14.0-1/input0",
                "vendor_id": "1234",
                "product_id": "5678",
                "device_type": "mouse",
                "device_types": ["mouse"],
            },
            {
                "path": "/dev/input/event28",
                "name": "Logitech G920 Driving Force Racing Wheel for Xbox One",
                "phys": "py-evdev-uinput",
                "vendor_id": "046d",
                "product_id": "c262",
                "device_type": "gamepad",
                "device_types": ["gamepad"],
            },
            {
                "path": "/dev/input/event11",
                "name": "Configured Keyboard",
                "phys": "usb-0000:00:14.0-2/input0",
                "vendor_id": "9999",
                "product_id": "0001",
                "device_type": "keyboard",
                "device_types": ["keyboard"],
            },
        ]
        monkeypatch.setattr(
            hardware_setup_mod,
            "session_request",
            lambda _payload, timeout=3.0: {
                "status": "ok",
                "devices": list(session_devices),
            },
        )

        hardware_manager = SimpleNamespace(
            get_hardware=lambda hardware_id: object() if hardware_id == "9999:0001" else None
        )
        dialog = HardwareSetupDialog(Gtk.Window(), hardware_manager)
        detected_devices: dict[str, dict] = {}

        assert dialog._detect_devices_via_session(detected_devices) is True
        assert detected_devices == {
            "1234:5678": {
                "name": "Real USB Mouse",
                "display_name": "Real USB Mouse",
                "hardware_id": "1234:5678",
                "model_id": "1234:5678",
                "vendor_id": "1234",
                "product_id": "5678",
                "paths": ["/dev/input/event10"],
                "interfaces": [
                    {
                        "path": "/dev/input/event10",
                        "stable_path": "/dev/input/event10",
                        "name": "Real USB Mouse",
                        "phys": "usb-0000:00:14.0-1/input0",
                        "device_type": DeviceType.MOUSE,
                        "device_types": ["mouse"],
                    }
                ],
            },
            "046d:c262": {
                "name": "Logitech G920 Driving Force Racing Wheel for Xbox One",
                "display_name": "Logitech G920 Driving Force Racing Wheel for Xbox One",
                "hardware_id": "046d:c262",
                "model_id": "046d:c262",
                "vendor_id": "046d",
                "product_id": "c262",
                "paths": ["/dev/input/event28"],
                "interfaces": [
                    {
                        "path": "/dev/input/event28",
                        "stable_path": "/dev/input/event28",
                        "name": "Logitech G920 Driving Force Racing Wheel for Xbox One",
                        "phys": "py-evdev-uinput",
                        "device_type": DeviceType.GAMEPAD,
                        "device_types": ["gamepad"],
                    }
                ],
            },
        }

    def test_local_detection_only_skips_uinput_devices_outside_raw_mode(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())
        device = SimpleNamespace(
            name="Logitech G920 Driving Force Racing Wheel for Xbox One",
            phys="py-evdev-uinput",
        )

        dialog._show_raw_evdev_devices = False
        assert dialog._should_skip_detected_device(device) is True

        dialog._show_raw_evdev_devices = True
        assert dialog._should_skip_detected_device(device) is False
        assert (
            dialog._should_skip_detected_device_info(
                {
                    "name": "Logitech G920 Driving Force Racing Wheel for Xbox One",
                    "phys": "py-evdev-uinput",
                }
            )
            is False
        )

    def test_detect_devices_via_session_skips_touchpads_but_keeps_other_interfaces(
        self, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import DeviceType
        from keymasq.gui.wizards import hardware_setup as hardware_setup_mod
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
        session_devices = [
            {
                "path": "/dev/input/event20",
                "name": "Integrated Touchpad",
                "phys": "i2c-ELAN1200:00",
                "vendor_id": "1234",
                "product_id": "5678",
                "device_type": "other",
                "device_types": ["touchpad"],
            },
            {
                "path": "/dev/input/event21",
                "name": "Integrated Keyboard",
                "phys": "isa0060/serio0/input0",
                "vendor_id": "1234",
                "product_id": "5678",
                "device_type": "keyboard",
                "device_types": ["keyboard"],
            },
            {
                "path": "/dev/input/event22",
                "name": "Standalone Touchpad",
                "phys": "i2c-SYNA2393:00",
                "vendor_id": "9999",
                "product_id": "0001",
                "device_type": "other",
                "device_types": ["touchpad"],
            },
        ]
        monkeypatch.setattr(
            hardware_setup_mod,
            "session_request",
            lambda _payload, timeout=3.0: {
                "status": "ok",
                "devices": list(session_devices),
            },
        )

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace(get_hardware=lambda _id: None))
        detected_devices: dict[str, dict] = {}

        assert dialog._detect_devices_via_session(detected_devices) is True
        assert detected_devices == {
            "1234:5678": {
                "name": "Integrated Keyboard",
                "display_name": "Integrated Keyboard",
                "hardware_id": "1234:5678",
                "model_id": "1234:5678",
                "vendor_id": "1234",
                "product_id": "5678",
                "paths": ["/dev/input/event21"],
                "interfaces": [
                    {
                        "path": "/dev/input/event21",
                        "stable_path": "/dev/input/event21",
                        "name": "Integrated Keyboard",
                        "phys": "isa0060/serio0/input0",
                        "device_type": DeviceType.KEYBOARD,
                        "device_types": ["keyboard"],
                    }
                ],
            }
        }

    def test_detect_devices_via_session_skips_configured_non_usb_phys(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.wizards import hardware_setup as hardware_setup_mod
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
        monkeypatch.setattr(
            hardware_setup_mod,
            "session_request",
            lambda _payload, timeout=3.0: {
                "status": "ok",
                "devices": [
                    {
                        "path": "/dev/input/event21",
                        "stable_path": "/dev/input/event21",
                        "name": "Integrated Keyboard",
                        "phys": "isa0060/serio0/input0",
                        "vendor_id": "1234",
                        "product_id": "5678",
                        "device_type": "keyboard",
                        "device_types": ["keyboard"],
                    }
                ],
            },
        )
        monkeypatch.setattr(
            hardware_setup_mod.evdev,
            "InputDevice",
            lambda _path: SimpleNamespace(
                phys="isa0060/serio0/input0",
                close=lambda: None,
            ),
        )
        configured = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Integrated Keyboard",
            evdev_devices=[
                EvdevDevice(path="/dev/input/event21", device_type=DeviceType.KEYBOARD)
            ],
            buttons=[],
        )
        hardware_manager = SimpleNamespace(list_hardware=lambda: [configured])
        dialog = HardwareSetupDialog(Gtk.Window(), hardware_manager)
        detected_devices: dict[str, dict] = {}

        assert dialog._detect_devices_via_session(detected_devices) is False
        assert detected_devices == {}

    def test_detect_devices_via_session_keeps_duplicate_gamepad_slots(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import DeviceType
        from keymasq.gui.wizards import hardware_setup as hardware_setup_mod
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
        session_devices = [
            {
                "path": "/dev/input/event20",
                "stable_path": "/dev/input/by-id/receiver-event-joystick",
                "name": "Xbox 360 Wireless Receiver",
                "phys": "usb-0000:0e:00.3-1.2/input0",
                "vendor_id": "045e",
                "product_id": "02a1",
                "device_type": "gamepad",
                "device_types": ["gamepad"],
            },
            {
                "path": "/dev/input/event21",
                "stable_path": "/dev/input/by-id/receiver-if02-event-joystick",
                "name": "Xbox 360 Wireless Receiver",
                "phys": "usb-0000:0e:00.3-1.2/input0",
                "vendor_id": "045e",
                "product_id": "02a1",
                "device_type": "gamepad",
                "device_types": ["gamepad"],
            },
        ]
        monkeypatch.setattr(
            hardware_setup_mod,
            "session_request",
            lambda _payload, timeout=3.0: {
                "status": "ok",
                "devices": list(session_devices),
            },
        )

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace(get_hardware=lambda _id: None))
        detected_devices: dict[str, dict] = {}

        assert dialog._detect_devices_via_session(detected_devices) is True

        assert set(detected_devices) == {
            "045e:02a1",
            "045e:02a1@2",
        }
        assert [len(device["interfaces"]) for device in detected_devices.values()] == [1, 1]
        assert {
            device["interfaces"][0]["device_type"]
            for device in detected_devices.values()
        } == {DeviceType.GAMEPAD}

    def test_detect_devices_via_session_keeps_uinput_gamepads_with_shared_phys_separate(
        self, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.wizards import hardware_setup as hardware_setup_mod
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
        monkeypatch.setattr(
            hardware_setup_mod,
            "session_request",
            lambda _payload, timeout=3.0: {
                "status": "ok",
                "devices": [
                    {
                        "path": "/dev/input/event29",
                        "stable_path": "/dev/input/event29",
                        "name": "Logitech G29 Driving Force Racing Wheel",
                        "phys": "py-evdev-uinput",
                        "vendor_id": "046d",
                        "product_id": "c24f",
                        "device_type": "gamepad",
                        "device_types": ["gamepad"],
                    },
                    {
                        "path": "/dev/input/event30",
                        "stable_path": "/dev/input/event30",
                        "name": "Virtgaming Xbox-Style Gamepad",
                        "phys": "py-evdev-uinput",
                        "vendor_id": "1234",
                        "product_id": "1001",
                        "device_type": "gamepad",
                        "device_types": ["gamepad"],
                    },
                ],
            },
        )

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace(get_hardware=lambda _id: None))
        detected_devices: dict[str, dict] = {}

        assert dialog._detect_devices_via_session(detected_devices) is True

        assert set(detected_devices) == {"046d:c24f", "1234:1001"}
        assert detected_devices["046d:c24f"]["name"] == "Logitech G29 Driving Force Racing Wheel"
        assert detected_devices["046d:c24f"]["paths"] == ["/dev/input/event29"]
        assert detected_devices["1234:1001"]["name"] == "Virtgaming Xbox-Style Gamepad"
        assert detected_devices["1234:1001"]["paths"] == ["/dev/input/event30"]

    def test_detect_devices_via_session_numbers_next_duplicate_gamepad_slot(
        self, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.wizards import hardware_setup as hardware_setup_mod
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        first_slot_path = "/dev/input/by-id/receiver-event-joystick"
        second_slot_path = "/dev/input/by-id/receiver-if02-event-joystick"
        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
        monkeypatch.setattr(
            hardware_setup_mod,
            "session_request",
            lambda _payload, timeout=3.0: {
                "status": "ok",
                "devices": [
                    {
                        "path": "/dev/input/event20",
                        "stable_path": first_slot_path,
                        "name": "Xbox 360 Wireless Receiver",
                        "phys": "usb-0000:0e:00.3-1.2/input0",
                        "vendor_id": "045e",
                        "product_id": "02a1",
                        "device_type": "gamepad",
                        "device_types": ["gamepad"],
                    },
                    {
                        "path": "/dev/input/event21",
                        "stable_path": second_slot_path,
                        "name": "Xbox 360 Wireless Receiver",
                        "phys": "usb-0000:0e:00.3-1.2/input0",
                        "vendor_id": "045e",
                        "product_id": "02a1",
                        "device_type": "gamepad",
                        "device_types": ["gamepad"],
                    },
                ],
            },
        )
        configured = HardwareConfig(
            vendor_id="045e",
            product_id="02a1",
            name="Xbox 360 1",
            evdev_devices=[
                EvdevDevice(path=first_slot_path, device_type=DeviceType.GAMEPAD)
            ],
            buttons=[],
        )
        hardware_manager = SimpleNamespace(
            list_hardware_ids=lambda: ["045e:02a1"],
            list_hardware=lambda: [configured],
        )
        dialog = HardwareSetupDialog(Gtk.Window(), hardware_manager)
        detected_devices: dict[str, dict] = {}

        assert dialog._detect_devices_via_session(detected_devices) is True

        assert set(detected_devices) == {"045e:02a1@2"}
        assert detected_devices["045e:02a1@2"]["interfaces"][0]["stable_path"] == second_slot_path

    def test_detect_devices_via_session_skips_configured_by_path_alias(
        self, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.wizards import hardware_setup as hardware_setup_mod
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        configured_path = "/dev/input/by-path/pci-0000:00:14.0-usb-0:1-event-mouse"
        event_path = "/dev/input/event20"
        stable_path = "/dev/input/by-id/usb-Test_Mouse-event-mouse"
        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
        monkeypatch.setattr(
            hardware_setup_mod,
            "session_request",
            lambda _payload, timeout=3.0: {
                "status": "ok",
                "devices": [
                    {
                        "path": event_path,
                        "stable_path": stable_path,
                        "name": "Test Mouse",
                        "phys": "usb-0000:00:14.0-1/input0",
                        "vendor_id": "1234",
                        "product_id": "5678",
                        "device_type": "mouse",
                        "device_types": ["mouse"],
                    },
                ],
            },
        )
        monkeypatch.setattr(
            hardware_setup_mod.os.path,
            "realpath",
            lambda path: event_path if path == configured_path else path,
        )
        monkeypatch.setattr(
            hardware_setup_mod,
            "resolve_stable_path",
            lambda path: stable_path if path == event_path else path,
        )
        configured = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Test Mouse",
            evdev_devices=[
                EvdevDevice(path=configured_path, device_type=DeviceType.MOUSE)
            ],
            buttons=[],
        )
        hardware_manager = SimpleNamespace(
            list_hardware_ids=lambda: ["1234:5678"],
            list_hardware=lambda: [configured],
        )
        dialog = HardwareSetupDialog(Gtk.Window(), hardware_manager)
        detected_devices: dict[str, dict] = {}

        assert dialog._detect_devices_via_session(detected_devices) is False
        assert detected_devices == {}

    def test_detect_devices_via_session_keeps_no_by_id_usb_event_nodes_separate(
        self, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.wizards import hardware_setup as hardware_setup_mod
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
        monkeypatch.setattr(
            hardware_setup_mod,
            "session_request",
            lambda _payload, timeout=3.0: {
                "status": "ok",
                "devices": [
                    {
                        "path": "/dev/input/event10",
                        "name": "USB Mouse",
                        "phys": "usb-0000:00:14.0-1/input0",
                        "vendor_id": "1234",
                        "product_id": "5678",
                        "device_type": "mouse",
                        "device_types": ["mouse"],
                    },
                    {
                        "path": "/dev/input/event11",
                        "name": "USB Mouse",
                        "phys": "usb-0000:00:14.0-2/input0",
                        "vendor_id": "1234",
                        "product_id": "5678",
                        "device_type": "mouse",
                        "device_types": ["mouse"],
                    },
                ],
            },
        )

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace(get_hardware=lambda _id: None))
        detected_devices: dict[str, dict] = {}

        assert dialog._detect_devices_via_session(detected_devices) is True

        assert set(detected_devices) == {"1234:5678", "1234:5678@2"}
        assert detected_devices["1234:5678"]["paths"] == ["/dev/input/event10"]
        assert detected_devices["1234:5678@2"]["paths"] == ["/dev/input/event11"]

    def test_save_gamepad_config_builds_buttons_from_capabilities(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        import evdev
        from gi.repository import Gtk

        from keymasq.common.models import DeviceType
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        class _HardwareManager:
            def __init__(self) -> None:
                self.saved = []

            def save_hardware(self, config) -> None:
                self.saved.append(config)

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        hardware_manager = _HardwareManager()
        dialog = HardwareSetupDialog(Gtk.Window(), hardware_manager)
        dialog.selected_device = {
            "vendor_id": "1234",
            "product_id": "5678",
            "name": "Test Pad",
        }
        dialog.discovered_interfaces = {
            "joystick": {
                "id": "joystick",
                "stable_path": "/dev/input/by-id/test-pad",
                "path": "/dev/input/event10",
                "name": "Test Pad",
                "device_type": DeviceType.GAMEPAD,
                "device_types": ["gamepad"],
                "capabilities": ["btn_start", "btn_south", "btn_east"],
                "raw_capabilities": {
                    evdev.ecodes.EV_KEY: [
                        evdev.ecodes.BTN_START,
                        evdev.ecodes.BTN_SOUTH,
                        evdev.ecodes.BTN_EAST,
                    ]
                },
            }
        }
        emitted = []
        dialog.emit = lambda signal, config: emitted.append((signal, config))
        dialog.close = lambda: None

        dialog._save_gamepad_config()

        assert len(hardware_manager.saved) == 1
        saved = hardware_manager.saved[0]
        assert saved.id is None
        assert saved.evdev_devices[0].device_type == DeviceType.GAMEPAD
        assert [button.id for button in saved.buttons] == ["btn_start", "btn_east", "btn_south"]
        assert [button.label for button in saved.buttons] == ["Start", "B", "A"]
        assert [button.evdev_code for button in saved.buttons] == [
            evdev.ecodes.BTN_START,
            evdev.ecodes.BTN_EAST,
            evdev.ecodes.BTN_SOUTH,
        ]
        assert all(button.type == "gamepad" for button in saved.buttons)
        assert emitted == [("device-created", saved)]

    def test_save_gamepad_config_builds_sticks_and_triggers_from_abs_capabilities(
        self,
        monkeypatch,
    ):
        gi.require_version("Gtk", "4.0")
        import evdev
        from gi.repository import Gtk

        from keymasq.common.models import DeviceType
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        class _HardwareManager:
            def __init__(self) -> None:
                self.saved = []

            def save_hardware(self, config) -> None:
                self.saved.append(config)

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        hardware_manager = _HardwareManager()
        dialog = HardwareSetupDialog(Gtk.Window(), hardware_manager)
        dialog.selected_device = {
            "vendor_id": "1234",
            "product_id": "5678",
            "name": "Test Pad",
        }
        dialog.discovered_interfaces = {
            "joystick": {
                "id": "joystick",
                "stable_path": "/dev/input/by-id/test-pad",
                "path": "/dev/input/event10",
                "name": "Test Pad",
                "device_type": DeviceType.GAMEPAD,
                "device_types": ["gamepad"],
                "capabilities": [],
                "raw_capabilities": {
                    evdev.ecodes.EV_ABS: [
                        evdev.ecodes.ABS_X,
                        evdev.ecodes.ABS_Y,
                        evdev.ecodes.ABS_RX,
                        evdev.ecodes.ABS_RY,
                        evdev.ecodes.ABS_Z,
                        evdev.ecodes.ABS_RZ,
                    ]
                },
            }
        }
        dialog.emit = lambda _signal, _config: None
        dialog.close = lambda: None

        dialog._save_gamepad_config()

        saved = hardware_manager.saved[0]
        analogs = {analog.id: analog for analog in saved.analog_inputs}
        assert set(analogs) == {"left_stick", "right_stick", "left_trigger", "right_trigger"}
        assert analogs["left_trigger"].type == "axis"
        assert analogs["left_trigger"].axes[0].role == "x"
        assert analogs["left_trigger"].axes[0].evdev_code == evdev.ecodes.ABS_Z
        assert analogs["right_trigger"].type == "axis"
        assert analogs["right_trigger"].axes[0].evdev_code == evdev.ecodes.ABS_RZ

    def test_save_mouse_keyboard_config_builds_standard_template(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import DeviceType
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        class _HardwareManager:
            def __init__(self) -> None:
                self.saved = []

            def save_hardware(self, config) -> None:
                self.saved.append(config)

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        hardware_manager = _HardwareManager()
        dialog = HardwareSetupDialog(Gtk.Window(), hardware_manager)
        dialog.selected_device = {
            "vendor_id": "1234",
            "product_id": "5678",
            "name": "Combo Device",
        }
        dialog.discovered_interfaces = {
            "mouse": {
                "id": "mouse",
                "stable_path": "/dev/input/by-id/test-mouse",
                "path": "/dev/input/event10",
                "name": "Combo Mouse",
                "device_type": DeviceType.MOUSE,
                "device_types": ["mouse"],
                "capabilities": [
                    "btn_left",
                    "btn_right",
                    "btn_middle",
                    "btn_side",
                    "btn_extra",
                    "rel_wheel",
                ],
            },
            "kbd": {
                "id": "kbd",
                "stable_path": "/dev/input/by-id/test-kbd",
                "path": "/dev/input/event11",
                "name": "Combo Keyboard",
                "device_type": DeviceType.KEYBOARD,
                "device_types": ["keyboard"],
                "capabilities": ["key_a", "key_b"],
            },
        }
        emitted = []
        dialog.emit = lambda signal, config: emitted.append((signal, config))
        dialog.close = lambda: None

        dialog._save_mouse_keyboard_config()

        assert len(hardware_manager.saved) == 1
        saved = hardware_manager.saved[0]
        assert [device.id for device in saved.evdev_devices] == ["mouse", "kbd"]
        assert [device.device_type for device in saved.evdev_devices] == [
            DeviceType.MOUSE,
            DeviceType.KEYBOARD,
        ]
        assert [button.id for button in saved.buttons[:5]] == [
            "btn_left",
            "btn_right",
            "btn_middle",
            "btn_back",
            "btn_forward",
        ]
        assert [button.evdev for button in saved.buttons[:5]] == [
            "btn_left",
            "btn_right",
            "btn_middle",
            "btn_side",
            "btn_extra",
        ]
        assert all(button.source == "mouse" for button in saved.buttons[:5])
        assert [button.id for button in saved.buttons[5:7]] == ["wheel_up", "wheel_down"]
        assert [button.evdev for button in saved.buttons[5:7]] == ["rel_wheel", "rel_wheel"]
        assert [button.evdev_value for button in saved.buttons[5:7]] == [1, -1]
        assert all(button.source == "mouse" for button in saved.buttons[5:7])
        assert saved.buttons[7].source == "kbd"
        assert saved.buttons[7].id == "key_esc"
        assert emitted == [("device-created", saved)]

    def test_save_mouse_config_keeps_grouped_keyboard_interface(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import DeviceType
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        class _HardwareManager:
            def __init__(self) -> None:
                self.saved = []

            def save_hardware(self, config) -> None:
                self.saved.append(config)

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        hardware_manager = _HardwareManager()
        dialog = HardwareSetupDialog(Gtk.Window(), hardware_manager)
        dialog.selected_device = {
            "vendor_id": "1532",
            "product_id": "00b4",
            "name": "Razer Naga",
        }
        dialog.discovered_interfaces = {
            "mouse": {
                "id": "mouse",
                "stable_path": "/dev/input/by-id/usb-Razer_Naga-event-mouse",
                "path": "/dev/input/event5",
                "name": "Razer Naga",
                "device_type": DeviceType.MOUSE,
                "device_types": ["mouse"],
                "capabilities": ["btn_left", "btn_right", "rel_wheel"],
            },
            "kbd": {
                "id": "kbd",
                "stable_path": "/dev/input/by-id/usb-Razer_Naga-if02-event-kbd",
                "path": "/dev/input/event7",
                "name": "Razer Naga",
                "device_type": DeviceType.KEYBOARD,
                "device_types": ["keyboard"],
                "capabilities": ["key_a"],
            },
        }
        dialog.emit = lambda _signal, _config: None
        dialog.close = lambda: None

        dialog._save_mouse_config()

        saved = hardware_manager.saved[0]
        assert [device.id for device in saved.evdev_devices] == ["mouse", "kbd"]
        assert [device.device_type for device in saved.evdev_devices] == [
            DeviceType.MOUSE,
            DeviceType.KEYBOARD,
        ]
        assert [button.source for button in saved.buttons] == ["mouse"] * len(saved.buttons)

    def test_standard_mouse_template_adds_horizontal_wheel_when_supported(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        import evdev
        from gi.repository import Gtk

        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())
        buttons = dialog._build_standard_mouse_buttons(
            "mouse",
            include_horizontal=True,
        )

        wheel_buttons = [button for button in buttons if button.type == "wheel"]
        assert [button.id for button in wheel_buttons] == [
            "wheel_up",
            "wheel_down",
            "wheel_left",
            "wheel_right",
        ]
        assert [button.evdev_code for button in wheel_buttons] == [
            evdev.ecodes.REL_WHEEL,
            evdev.ecodes.REL_WHEEL,
            evdev.ecodes.REL_HWHEEL,
            evdev.ecodes.REL_HWHEEL,
        ]
        assert [button.evdev_value for button in wheel_buttons] == [1, -1, -1, 1]

    def test_keyboard_template_excludes_key_102nd(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())
        buttons = dialog._build_standard_keyboard_buttons("kbd")

        assert "key_102nd" not in [button.id for button in buttons]


def test_keyboard_device_tab_prepends_extra_buttons_section():
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
    from keymasq.gui.widgets.device_tab import DeviceTab

    def child_widgets(widget):
        items = []
        child = widget.get_first_child()
        while child is not None:
            items.append(child)
            child = child.get_next_sibling()
        return items

    template_buttons = [
        "key_esc",
        "key_1",
        "key_2",
        "key_3",
        "key_4",
        "key_5",
        "key_6",
        "key_7",
        "key_8",
        "key_9",
        "key_0",
        "key_minus",
        "key_equal",
        "key_backspace",
        "key_tab",
        "key_q",
        "key_w",
        "key_e",
        "key_r",
        "key_t",
        "key_y",
        "key_u",
        "key_i",
        "key_o",
        "key_p",
        "key_leftbrace",
        "key_rightbrace",
        "key_backslash",
        "key_capslock",
        "key_a",
        "key_s",
        "key_d",
        "key_f",
        "key_g",
        "key_h",
        "key_j",
        "key_k",
        "key_l",
        "key_semicolon",
        "key_apostrophe",
        "key_enter",
        "key_leftshift",
        "key_z",
        "key_x",
        "key_c",
        "key_v",
        "key_b",
        "key_n",
        "key_m",
        "key_comma",
        "key_dot",
        "key_slash",
        "key_rightshift",
        "key_leftctrl",
        "key_leftmeta",
        "key_leftalt",
        "key_space",
        "key_rightalt",
        "key_rightctrl",
        "key_rightmeta",
    ]

    buttons = [
        ButtonDefinition(id=key_id, label=key_id.upper(), evdev=key_id, source="kbd")
        for key_id in template_buttons
    ]
    buttons = [
        ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left", source="mouse"),
        ButtonDefinition(id="btn_back", label="Back", evdev="btn_side", source="mouse"),
        *buttons,
    ]

    tab = DeviceTab(
        HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Combo Device",
            evdev_devices=[
                EvdevDevice(path="/dev/input/event10", device_type=DeviceType.MOUSE, id="mouse"),
                EvdevDevice(path="/dev/input/event11", device_type=DeviceType.KEYBOARD, id="kbd"),
            ],
            buttons=buttons,
        ),
        profile_manager=None,
        demo_mode=True,
    )

    scrolled = child_widgets(tab)[-1]
    content = scrolled.get_child()
    if not isinstance(content, Gtk.Box):
        content = content.get_child()
    first_section = child_widgets(content)[0]

    assert isinstance(first_section, Gtk.Expander)
    assert first_section.get_label() == "Extra Buttons (2)"
    assert first_section.get_expanded() is True


def test_hardware_setup_saves_standard_keyboard_template(monkeypatch):
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.models import DeviceType
    from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

    class _HardwareManager:
        def __init__(self) -> None:
            self.saved = []

        def save_hardware(self, config) -> None:
            self.saved.append(config)

    monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
    hardware_manager = _HardwareManager()
    dialog = HardwareSetupDialog(Gtk.Window(), hardware_manager)
    dialog.selected_device = {
        "vendor_id": "1234",
        "product_id": "5678",
        "name": "Keyboard",
    }
    dialog.discovered_interfaces = {
        "kbd": {
            "id": "kbd",
            "stable_path": "/dev/input/by-id/test-kbd",
            "device_types": ["keyboard"],
        }
    }
    emitted = []
    dialog.emit = lambda signal, config: emitted.append((signal, config))
    dialog.close = lambda: None

    dialog._save_keyboard_config()

    saved = hardware_manager.saved[0]
    assert saved.evdev_devices[0].device_type == DeviceType.KEYBOARD
    assert saved.evdev_devices[0].id == "kbd"
    assert saved.buttons[0].id == "key_esc"
    assert saved.buttons[1].id == "key_grave"
    assert saved.buttons[0].source == "kbd"
    assert {button.id for button in saved.buttons} >= {
        "key_grave",
        "key_space",
        "key_leftctrl",
        "key_rightmeta",
        "key_f12",
        "key_kpenter",
    }
    assert emitted == [("device-created", saved)]


def test_hardware_setup_saves_mouse_keyboard_template_with_horizontal_wheel(monkeypatch):
    gi.require_version("Gtk", "4.0")
    import evdev
    from gi.repository import Gtk

    from keymasq.common.models import DeviceType
    from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

    class _HardwareManager:
        def __init__(self) -> None:
            self.saved = []

        def save_hardware(self, config) -> None:
            self.saved.append(config)

    monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
    hardware_manager = _HardwareManager()
    dialog = HardwareSetupDialog(Gtk.Window(), hardware_manager)
    dialog.selected_device = {
        "vendor_id": "1234",
        "product_id": "5678",
        "name": "Combo",
    }
    dialog.discovered_interfaces = {
        "mouse": {
            "id": "mouse",
            "stable_path": "/dev/input/by-id/test-mouse",
            "device_types": ["mouse"],
            "capabilities": ["btn_left"],
            "raw_capabilities": {evdev.ecodes.EV_REL: [evdev.ecodes.REL_HWHEEL]},
        },
        "kbd": {
            "id": "kbd",
            "stable_path": "/dev/input/by-id/test-kbd",
            "device_types": ["keyboard"],
            "capabilities": ["key_a"],
        },
    }
    dialog.emit = lambda _signal, _config: None
    dialog.close = lambda: None

    dialog._save_mouse_keyboard_config()

    saved = hardware_manager.saved[0]
    assert [device.id for device in saved.evdev_devices] == ["mouse", "kbd"]
    assert [device.device_type for device in saved.evdev_devices] == [
        DeviceType.MOUSE,
        DeviceType.KEYBOARD,
    ]
    by_id = {button.id: button for button in saved.buttons}
    assert by_id["btn_left"].source == "mouse"
    assert by_id["wheel_left"].source == "mouse"
    assert by_id["wheel_right"].source == "mouse"
    assert by_id["key_a"].source == "kbd"


def test_hardware_setup_capture_flow_records_buttons_and_saves(monkeypatch):
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    import keymasq.gui.wizards.hardware_setup as hardware_setup_module
    from keymasq.common.models import DeviceType
    from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

    class _HardwareManager:
        def __init__(self) -> None:
            self.saved = []

        def save_hardware(self, config) -> None:
            self.saved.append(config)

    requests: list[dict] = []

    def fake_session_request_async(payload, callback):
        requests.append(payload)
        if payload["command"] == "begin_capture":
            callback({"status": "ok", "warnings": ["limited permissions"]})
        elif payload["command"] == "capture_read":
            callback(
                {
                    "status": "ok",
                    "captured": {
                        "evdev": "btn_left",
                        "code": 272,
                        "value": 1,
                        "direction": "press",
                        "source": "mouse",
                        "stable_path": "/dev/input/by-id/test-mouse",
                    },
                }
            )
        elif payload["command"] == "end_capture":
            callback({"status": "ok"})

    monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
    monkeypatch.setattr(hardware_setup_module, "session_request_async", fake_session_request_async)
    monkeypatch.setattr(hardware_setup_module.GLib, "timeout_add", lambda *_args: 42)
    monkeypatch.setattr(hardware_setup_module.GLib, "source_remove", lambda _source_id: True)

    hardware_manager = _HardwareManager()
    dialog = HardwareSetupDialog(Gtk.Window(), hardware_manager)
    dialog._setup_page_capture()
    dialog.selected_device = {
        "vendor_id": "1234",
        "product_id": "5678",
        "name": "Capture Mouse",
    }
    dialog.discovered_interfaces = {
        "mouse": {
            "id": "mouse",
            "stable_path": "/dev/input/by-id/test-mouse",
            "device_types": ["mouse"],
            "capabilities": ["btn_left", "rel_x"],
        }
    }
    dialog._capture_hardware_id = "1234:5678"
    dialog.button_definitions = [
        {"id": "btn_left", "label": "Left Click", "type": "button"},
    ]
    dialog.current_button_index = 0
    dialog.emit = lambda _signal, _config: None
    dialog.close = lambda: None

    dialog._update_capture_ui()
    dialog._on_start_capture(dialog.capture_btn)

    assert dialog.capture_status.get_label() == "Capture warnings: limited permissions"
    assert dialog._capture_poll_id == 42

    assert dialog._poll_capture() is True

    assert dialog.current_button_index == 1
    assert dialog.capture_title.get_label() == "Setup Complete!"
    assert dialog.capture_btn.get_label() == "Save"
    assert dialog.button_definitions[0]["evdev"] == "btn_left"
    assert dialog.button_definitions[0]["source"] == "mouse"

    dialog._on_save(dialog.capture_btn)

    saved = hardware_manager.saved[0]
    assert saved.id is None
    assert saved.evdev_devices[0].device_type == DeviceType.MOUSE
    assert saved.evdev_devices[0].capabilities == ["btn_left", "rel_x"]
    assert saved.buttons[0].id == "btn_left"
    assert saved.buttons[0].evdev == "btn_left"
    assert requests == [
        {
            "command": "begin_capture",
            "hardware_id": "1234:5678",
            "end_on_disconnect": True,
            "evdev_paths": ["/dev/input/by-id/test-mouse"],
            "evdev_interfaces": [
                    {
                        "id": "mouse",
                        "path": "/dev/input/by-id/test-mouse",
                        "type": "mouse",
                        "phys": "",
                        "capabilities": ["btn_left", "rel_x"],
                    }
                ],
            },
        {"command": "capture_read", "hardware_id": "1234:5678"},
        {"command": "end_capture", "hardware_id": "1234:5678"},
    ]
