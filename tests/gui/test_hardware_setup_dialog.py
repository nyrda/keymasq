# ruff: noqa: F403, F405, I001
from tests.gui.support import *

class TestHardwareSetupDialog:
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

    def test_detect_devices_via_session_skips_virtual_uinput_devices(self, monkeypatch):
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
                "path": "/dev/input/event10",
                "name": "Real USB Mouse",
                "phys": "usb-0000:00:14.0-1/input0",
                "vendor_id": "1234",
                "product_id": "5678",
                "device_type": "mouse",
                "device_types": ["mouse"],
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
            }
        }

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

    def test_detect_devices_via_session_ignores_unstable_usb_phys_for_identity(
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

        assert set(detected_devices) == {"1234:5678"}
        assert len(detected_devices["1234:5678"]["interfaces"]) == 2

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
        assert analogs["left_trigger"].type == "trigger"
        assert analogs["left_trigger"].axes[0].role == "x"
        assert analogs["left_trigger"].axes[0].evdev_code == evdev.ecodes.ABS_Z
        assert analogs["right_trigger"].type == "trigger"
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
    assert saved.buttons[0].id == "btn_left"
    assert saved.buttons[0].evdev == "btn_left"
    assert requests == [
        {
            "command": "begin_capture",
            "hardware_id": "1234:5678",
            "evdev_paths": ["/dev/input/by-id/test-mouse"],
        },
        {"command": "capture_read", "hardware_id": "1234:5678"},
        {"command": "end_capture", "hardware_id": "1234:5678"},
    ]
