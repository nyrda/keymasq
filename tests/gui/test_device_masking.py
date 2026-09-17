from pathlib import Path
from types import SimpleNamespace

import pytest

from keymasq.gui.widgets.device_masking import resolve_masking_devices
from keymasq.gui.widgets.hardware_masking_dialog import HardwareMaskingPanel
from keymasq.masking.inventory import Attachment, HardwareInventory
from tests.common.test_hardware_masking import write
from tests.gui.test_hardware_masking_dialog import response


def test_physical_matching_distinguishes_identical_receivers_and_groups_interfaces(
    tmp_path, monkeypatch
):
    inventory = HardwareInventory(tmp_path / "sys", tmp_path / "dev")
    attachments = []
    paths = []
    for number in range(3):
        receiver = number // 2
        parent = inventory.sys_root / f"devices/usb/1-{receiver + 1}"
        event = parent / f"input/input{number}"
        write(event / "phys", f"usb-receiver-{receiver}/input{number}")
        write(event / "id/vendor", "045e")
        write(event / "id/product", "02a1")
        alias = inventory.sys_root / f"class/input/event{number}"
        alias.mkdir(parents=True)
        (alias / "device").symlink_to(event)
        path = inventory.dev_root / f"input/event{number}"
        write(path, "")
        paths.append(str(path))
        if number in {0, 2}:
            attachments.append(
                Attachment(
                    str(receiver), "gen", "Receiver", "045e", "0719", "usb", parent, parent.name
                )
            )
    monkeypatch.setattr(inventory, "scan", lambda: attachments)
    assert resolve_masking_devices([(paths[0], ""), (paths[1], "")], inventory) == {"0"}
    assert resolve_masking_devices([(paths[2], "")], inventory) == {"1"}
    assert resolve_masking_devices([("keymasq:045e:02a1", "")], inventory) == set()
    assert resolve_masking_devices([("keymasq:045e:02a1", "usb-receiver-1/input2")], inventory) == {
        "1"
    }
    # A disconnected exact selector must not fall back to another matching model.
    assert (
        resolve_masking_devices([("keymasq:045e:02a1", "usb-receiver-missing/input0")], inventory)
        == set()
    )
    stable = inventory.dev_root / "input/by-id/controller"
    stable.parent.mkdir()
    stable.symlink_to(Path(paths[2]))
    assert resolve_masking_devices([(str(stable), "")], inventory) == {"1"}
    assert (
        resolve_masking_devices(
            [(str(stable.parent / "missing"), "usb-receiver-1/input2")], inventory
        )
        == set()
    )

    hid = attachments[1].syspath / "hid"
    write(hid / "uevent", "HID_PHYS=native-source\n")
    alias = inventory.sys_root / "bus/hid/devices/native"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(hid)
    assert resolve_masking_devices(
        [("/dev/keymasq-sources/motion/native/hidraw0", "native-source")], inventory
    ) == {"1"}


@pytest.mark.parametrize("source_kind", ["stable", "phys", "native"])
@pytest.mark.parametrize("replacement", [("1234", "9999"), ("9999", "5678")])
def test_settings_mask_target_rejects_reused_path_with_different_model(
    tmp_path, monkeypatch, source_kind, replacement
):
    inventory = HardwareInventory(tmp_path / "sys", tmp_path / "dev")
    parent = inventory.sys_root / "devices/usb/1-1"
    hid = parent / "0003:1234:5678.0001"
    event = hid / "input/input0"
    write(event / "phys", "receiver/input0")
    alias = inventory.sys_root / "class/input/event0/device"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(event)
    hid_alias = inventory.sys_root / "bus/hid/devices" / hid.name
    hid_alias.parent.mkdir(parents=True)
    hid_alias.symlink_to(hid)
    node = inventory.dev_root / "input/event0"
    write(node, "")
    stable = inventory.dev_root / "input/by-id/controller"
    stable.parent.mkdir()
    stable.symlink_to(node)
    source = {
        "stable": (str(stable), ""),
        "phys": (str(node), "receiver/input0"),
        "native": (f"/dev/keymasq-sources/motion/{hid.name}/hidraw0", "receiver/input0"),
    }[source_kind]
    if source_kind == "native":
        alias.unlink()
    # The receiver's USB identity can differ from its input identity.
    attachment = Attachment("receiver", "gen", "Receiver", "abcd", "9876", "usb", parent, "1-1")
    monkeypatch.setattr(inventory, "scan", lambda: [attachment])

    for vendor, product in [("1234", "5678"), replacement]:
        write(event / "id/vendor", vendor)
        write(event / "id/product", product)
        write(hid / "uevent", f"HID_PHYS=receiver/input0\nHID_ID=0003:0000{vendor}:0000{product}\n")
        selected = resolve_masking_devices([source], inventory, hardware_id="1234:5678@2")
        assert selected == ({"receiver"} if (vendor, product) == ("1234", "5678") else set())
        assert resolve_masking_devices([source], inventory, hardware_id=f"{vendor}:{product}") == {
            "receiver"
        }


def test_setup_defers_changes_until_save_and_authenticates_once(monkeypatch):
    requests = []
    unlocks = []
    parent = SimpleNamespace(
        _recording_unlocked=False,
        present_unlock_dialog=lambda **kwargs: unlocks.append(kwargs["on_success"]),
    )
    panel = HardwareMaskingPanel(parent, identities={"first", "second"}, deferred=True)
    monkeypatch.setattr(panel, "_change", lambda *args: requests.append(args))
    panel._render(response())
    assert set(panel._rows) == {"first", "second"}
    for row in panel._rows.values():
        row.switch.set_active(True)
    assert not requests
    assert not unlocks
    assert panel.apply_choices()
    assert len(unlocks) == 1
    assert not requests
    parent._recording_unlocked = True
    unlocks[0]()
    assert {data["id"] for _, data in requests} == {"first", "second"}


def test_cancelled_setup_never_applies_pending_changes(monkeypatch):
    requests = []
    panel = HardwareMaskingPanel(identities={"first"}, deferred=True)
    monkeypatch.setattr(panel, "_change", lambda *args: requests.append(args))
    panel._render(response())
    panel._rows["first"].switch.set_active(True)
    panel.close()
    assert not requests


def test_setup_saves_hardware_before_masking_and_stays_open_for_confirmation(monkeypatch):
    from gi.repository import Gtk

    from keymasq.common.model.hardware import HardwareConfig
    from keymasq.gui.wizards.hardware_setup.dialog import HardwareSetupDialog

    events = []
    monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
    parent = Gtk.Window()
    parent._recording_unlocked = True
    manager = SimpleNamespace(save_hardware=lambda config: events.append(("save", config)))
    wizard = HardwareSetupDialog(parent, manager)
    wizard.connect("device-created", lambda _wizard, config: events.append(("created", config)))
    monkeypatch.setattr(wizard, "close", lambda: events.append(("closed", None)))
    monkeypatch.setattr(wizard.masking, "_change", lambda *args: events.append(("mask", args)))
    wizard.masking.identities = {"first"}
    wizard.masking._render(response())
    wizard.masking._rows["first"].switch.set_active(True)
    config = HardwareConfig("1234", "5678", "Controller", [], [])
    wizard._persist_config(config)
    assert [event for event, _ in events] == ["save", "created", "mask"]
    assert config.masking_devices == ["first"]
    assert wizard.next_btn.get_label() == "Done"
    wizard._on_next(wizard.next_btn)
    assert events[-1][0] == "closed"
    wizard.masking.close()


def test_closed_settings_does_not_apply_after_association_save(monkeypatch):
    requests = []
    callbacks = []
    panel = HardwareMaskingPanel(
        SimpleNamespace(_recording_unlocked=True),
        identities={"first"},
        remember=lambda _identity, proceed: callbacks.append(proceed),
    )
    from keymasq.gui.widgets import hardware_masking_dialog as module

    monkeypatch.setattr(
        module, "session_request_async", lambda *args, **kwargs: requests.append(args)
    )
    panel._render(response())
    panel._rows["first"].switch.set_active(True)
    assert len(callbacks) == 1
    panel.close()
    callbacks[0]()
    assert not requests
