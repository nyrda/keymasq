from types import SimpleNamespace

from keymasq.keymasqd.device_inventory import (
    recording_grabbed_source_metadata,
    recording_virtual_device_metadata,
)


def test_inventory_exposes_grab_calibration_for_source_and_passthrough():
    device = SimpleNamespace(
        hardware_id="1234:5678",
        interface_id="stick",
        stable_path="/dev/input/event1",
        path="/dev/input/event1",
        uinput=SimpleNamespace(device=SimpleNamespace(path="/dev/input/event2")),
        analog_axis_calibrations={
            ("throttle", "x"): {"minimum": 0, "maximum": 255, "rest": 37},
        },
    )
    grabbed = {device.hardware_id: [device]}
    source = recording_grabbed_source_metadata(grabbed)[device.stable_path]
    passthrough = recording_virtual_device_metadata(SimpleNamespace(), grabbed)["/dev/input/event2"]
    expected = {"throttle": {"x": {"minimum": 0, "maximum": 255, "rest": 37}}}
    assert source["analog_calibration"] == expected
    assert passthrough["analog_calibration"] == expected
