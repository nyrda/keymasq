import pytest

from keymasq.common.model.core import DeviceType
from keymasq.session.hardware import HardwareManager

gi = pytest.importorskip("gi")


@pytest.fixture(autouse=True)
def only_bundled_presets(temp_config_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "share"))


class _RecordingHardwareManager(HardwareManager):
    def __init__(self) -> None:
        super().__init__()
        self.saved = []

    def save_hardware(self, config) -> None:
        self.saved.append(config)


def _deck_dialog(monkeypatch, *, product_id: str = "1205"):
    gi.require_version("Gtk", "4.0")
    import evdev
    from gi.repository import Gtk

    from keymasq.gui.wizards.hardware_setup import dialog as dialog_module
    from keymasq.gui.wizards.hardware_setup.dialog import HardwareSetupDialog

    def run_now(worker, callback, on_done=None):
        callback(dialog_module.GuiTaskResult(value=worker()))
        if on_done:
            on_done()

    monkeypatch.setattr(dialog_module, "run_gui_task", run_now)
    monkeypatch.setattr(HardwareSetupDialog, "_collect_detected_devices", lambda self: {})
    codes = evdev.ecodes
    pad = evdev.AbsInfo(0, -32767, 32767, 0, 0, 0)
    trigger = evdev.AbsInfo(0, 0, 32767, 0, 0, 0)
    dialog = HardwareSetupDialog(Gtk.Window(), _RecordingHardwareManager())
    dialog._discovery_state.selected_device = {
        "vendor_id": "28de",
        "product_id": product_id,
        "name": "Steam Deck",
    }
    dialog._discovery_state.discovered_interfaces = {
        "joystick": {
            "id": "joystick",
            "stable_path": "/dev/input/by-id/deck-joystick",
            "path": "/dev/input/event10",
            "name": "Steam Deck",
            "device_type": DeviceType.GAMEPAD,
            "device_types": ["gamepad"],
            "capabilities": [],
            "raw_capabilities": {
                codes.EV_KEY: [codes.BTN_SOUTH, codes.BTN_TL, codes.BTN_THUMB, codes.BTN_BASE],
                codes.EV_ABS: [
                    (codes.ABS_X, pad),
                    (codes.ABS_Y, pad),
                    (codes.ABS_HAT0X, pad),
                    (codes.ABS_HAT0Y, pad),
                    (codes.ABS_HAT1X, pad),
                    (codes.ABS_HAT1Y, pad),
                    (codes.ABS_HAT2X, trigger),
                    (codes.ABS_HAT2Y, trigger),
                ],
            },
        },
        "kbd": {
            "id": "kbd",
            "stable_path": "/dev/input/by-id/deck-kbd",
            "device_types": ["keyboard"],
        },
    }
    dialog._refresh_configure_modes()
    dialog.emit = lambda _signal, _config: None
    dialog.close = lambda: None
    return dialog


def _saved_layout(dialog) -> dict[str, tuple[str, list[tuple[str, bool]]]]:
    saved = dialog.hardware_manager.saved[0]
    return {
        analog.id: (analog.type, [(axis.evdev, axis.invert) for axis in analog.axes])
        for analog in saved.analog_inputs
    }


def test_adding_a_steam_deck_uses_the_bundled_hardware_preset_by_default(monkeypatch):
    dialog = _deck_dialog(monkeypatch)

    assert dialog.hardware_preset_row.get_visible()
    dialog._save_gamepad_config()

    assert _saved_layout(dialog) == {
        "left_stick": ("stick", [("abs_x", False), ("abs_y", False)]),
        "left_trackpad": ("stick", [("abs_hat0x", False), ("abs_hat0y", True)]),
        "right_trackpad": ("stick", [("abs_hat1x", False), ("abs_hat1y", True)]),
        "left_trigger": ("axis", [("abs_hat2y", False)]),
        "right_trigger": ("axis", [("abs_hat2x", False)]),
    }
    labels = {button.id: button.label for button in dialog.hardware_manager.saved[0].buttons}
    assert labels == {
        "btn_south": "A",
        "btn_tl": "L1",
        "btn_thumb": "Left Trackpad Click",
        "btn_base": "Quick Access",
    }


def test_choosing_no_hardware_preset_keeps_the_detected_controls(monkeypatch):
    dialog = _deck_dialog(monkeypatch)

    dialog.hardware_preset_combo.set_selected(dialog.hardware_preset_model.get_n_items() - 1)
    dialog._save_gamepad_config()

    assert set(_saved_layout(dialog)) == {"left_stick", "hat_0", "hat_1", "hat_2"}


def test_a_hardware_preset_is_offered_only_for_its_model_and_device_type(monkeypatch):
    other_model = _deck_dialog(monkeypatch, product_id="9999")
    assert not other_model.hardware_preset_row.get_visible()

    deck = _deck_dialog(monkeypatch)
    deck.mode_combo.set_selected(deck._template_state.values.index("keyboard"))
    assert not deck.hardware_preset_row.get_visible()
    deck._save_keyboard_config()
    assert deck.hardware_manager.saved[0].buttons[0].id == "key_esc"
