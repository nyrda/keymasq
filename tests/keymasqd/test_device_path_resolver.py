from types import SimpleNamespace

import evdev

from keymasq.common.devices import detect_input_classes, primary_input_class
from keymasq.common.model.core import DeviceType
from keymasq.keymasqd.runtime.device_path_resolver import (
    DeviceCache,
    DevicePathResolverDeps,
    evdev_device_path_resolver_deps,
    resolve_evdev_interfaces,
)


class _FakeDevice:
    def __init__(
        self,
        path: str,
        *,
        vendor: int = 0x2DC8,
        product: int = 0x3106,
        name: str = "Bluetooth Pad",
        phys: str = "bluetooth/input0",
        caps: dict[int, list[int]] | None = None,
    ) -> None:
        self.path = path
        self.info = SimpleNamespace(vendor=vendor, product=product)
        self.name = name
        self.phys = phys
        self._caps = caps or {
            evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
            evdev.ecodes.EV_ABS: [evdev.ecodes.ABS_X, evdev.ecodes.ABS_Y],
        }
        self.close_count = 0

    def capabilities(self):
        return self._caps

    def input_props(self):
        return []

    def close(self) -> None:
        self.close_count += 1


def _resolve(
    interfaces,
    devices,
    hardware_id: str | None = None,
    excluded_paths=None,
    preferred_paths=None,
    resolve_stable_path_fn=None,
    match_model_gamepads: bool = False,
):
    cache = DeviceCache()
    cache.refresh_sync(
        device_paths_fn=lambda: list(devices),
        device_input_fn=lambda path: devices[path],
        detect_input_classes_fn=detect_input_classes,
        primary_input_class_fn=primary_input_class,
    )
    return resolve_evdev_interfaces(
        interfaces,
        deps=DevicePathResolverDeps(
            device_paths_fn=lambda: list(devices),
            device_input_fn=lambda path: devices[path],
            detect_input_classes_fn=detect_input_classes,
            primary_input_class_fn=primary_input_class,
            resolve_stable_path_fn=resolve_stable_path_fn,
            cache=cache,
        ),
        hardware_id=hardware_id,
        excluded_paths=excluded_paths,
        preferred_paths=preferred_paths,
        match_model_gamepads=match_model_gamepads,
    )


def test_literal_path_resolves_unchanged() -> None:
    resolved = _resolve(
        [{"id": "gamepad", "path": "/dev/input/by-id/test", "type": "gamepad"}],
        {},
    )

    assert resolved[0].path == "/dev/input/by-id/test"
    assert resolved[0].interface_id == "gamepad"


def test_literal_gamepad_path_resolves_unchanged_when_model_matching_enabled() -> None:
    devices = {
        "/dev/input/event2": _FakeDevice("/dev/input/event2"),
    }

    resolved = _resolve(
        [
            {
                "id": "gamepad",
                "path": "/dev/input/by-id/test-pad-event-joystick",
                "type": "gamepad",
            }
        ],
        devices,
        hardware_id="2dc8:3106",
        match_model_gamepads=True,
    )

    assert [interface.path for interface in resolved] == [
        "/dev/input/by-id/test-pad-event-joystick"
    ]


def test_evdev_device_path_resolver_deps_wires_shared_helpers(monkeypatch) -> None:
    def device_input(path: str) -> _FakeDevice:
        return _FakeDevice(path)

    monkeypatch.setattr(evdev, "list_devices", lambda: ["/dev/input/event1"])

    deps = evdev_device_path_resolver_deps(device_input)

    assert deps.device_paths_fn() == ["/dev/input/event1"]
    assert deps.device_input_fn is device_input
    assert deps.detect_input_classes_fn(_FakeDevice("/dev/input/event1")) == ["gamepad"]
    assert deps.primary_input_class_fn(["keyboard"]) == DeviceType.KEYBOARD
    assert deps.resolve_stable_path_fn is not None


def test_keymasq_path_resolves_matching_vid_pid() -> None:
    devices = {
        "/dev/input/event1": _FakeDevice("/dev/input/event1", vendor=0x9999),
        "/dev/input/event2": _FakeDevice("/dev/input/event2"),
    }

    resolved = _resolve(
        [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}],
        devices,
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event2"]
    assert devices["/dev/input/event1"].close_count == 1
    assert devices["/dev/input/event2"].close_count == 1


def test_keymasq_path_uses_cached_probe_metadata() -> None:
    devices = {
        "/dev/input/event2": _FakeDevice("/dev/input/event2"),
    }
    cache = DeviceCache()
    cache.refresh_sync(
        device_paths_fn=lambda: list(devices),
        device_input_fn=lambda path: devices[path],
        detect_input_classes_fn=detect_input_classes,
        primary_input_class_fn=primary_input_class,
    )

    def fail_input_device(_path: str):
        raise AssertionError("resolver must not probe devices on request path")

    resolved = resolve_evdev_interfaces(
        [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}],
        deps=DevicePathResolverDeps(
            device_paths_fn=lambda: list(devices),
            device_input_fn=fail_input_device,
            detect_input_classes_fn=detect_input_classes,
            primary_input_class_fn=primary_input_class,
            cache=cache,
        ),
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event2"]
    assert devices["/dev/input/event2"].close_count == 1


def test_keymasq_path_probes_device_when_cache_misses() -> None:
    devices = {
        "/dev/input/event2": _FakeDevice("/dev/input/event2"),
    }

    resolved = resolve_evdev_interfaces(
        [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}],
        deps=DevicePathResolverDeps(
            device_paths_fn=lambda: list(devices),
            device_input_fn=lambda path: devices[path],
            detect_input_classes_fn=detect_input_classes,
            primary_input_class_fn=primary_input_class,
            cache=DeviceCache(),
        ),
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event2"]
    assert devices["/dev/input/event2"].close_count == 1


def test_keymasq_path_closes_devices_after_scan_errors() -> None:
    class _FailingDevice(_FakeDevice):
        def capabilities(self):
            raise OSError("capability read failed")

    devices = {
        "/dev/input/event1": _FakeDevice("/dev/input/event1", vendor=0x9999),
        "/dev/input/event2": _FailingDevice("/dev/input/event2"),
        "/dev/input/event3": _FakeDevice("/dev/input/event3"),
    }

    resolved = _resolve(
        [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}],
        devices,
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event3"]
    assert {path: device.close_count for path, device in devices.items()} == {
        "/dev/input/event1": 1,
        "/dev/input/event2": 1,
        "/dev/input/event3": 1,
    }


def test_keymasq_path_skips_virtual_devices() -> None:
    devices = {
        "/dev/input/event1": _FakeDevice("/dev/input/event1", phys="py-evdev-uinput"),
        "/dev/input/event2": _FakeDevice("/dev/input/event2"),
    }

    resolved = _resolve(
        [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}],
        devices,
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event2"]


def test_type_and_capability_scores_choose_best_candidate() -> None:
    keyboard_caps = {evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A]}
    weak_gamepad_caps = {
        evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_NORTH],
        evdev.ecodes.EV_ABS: [evdev.ecodes.ABS_X],
    }
    best_gamepad_caps = {
        evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH, evdev.ecodes.BTN_EAST],
        evdev.ecodes.EV_ABS: [evdev.ecodes.ABS_X],
    }
    devices = {
        "/dev/input/event1": _FakeDevice("/dev/input/event1", caps=keyboard_caps),
        "/dev/input/event2": _FakeDevice("/dev/input/event2", caps=weak_gamepad_caps),
        "/dev/input/event3": _FakeDevice("/dev/input/event3", caps=best_gamepad_caps),
    }

    resolved = _resolve(
        [
            {
                "id": "gamepad",
                "path": "keymasq:2dc8:3106",
                "type": DeviceType.GAMEPAD.value,
                "capabilities": ["btn_south", "btn_east"],
            }
        ],
        devices,
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event3"]


def test_metadata_descriptor_does_not_pick_same_vid_pid_without_any_match() -> None:
    keyboard_caps = {evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A]}
    devices = {
        "/dev/input/event1": _FakeDevice(
            "/dev/input/event1",
            caps=keyboard_caps,
            phys="bluetooth/input1",
        ),
    }

    resolved = _resolve(
        [
            {
                "id": "gamepad",
                "path": "keymasq:2dc8:3106",
                "type": DeviceType.GAMEPAD.value,
                "phys": "bluetooth/input0",
                "capabilities": ["btn_south", "abs_x"],
            }
        ],
        devices,
    )

    assert resolved == []


def test_other_type_descriptor_requires_phys_or_capability_match() -> None:
    keyboard_caps = {evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A]}
    devices = {
        "/dev/input/event1": _FakeDevice(
            "/dev/input/event1",
            caps=keyboard_caps,
            phys="bluetooth/input1",
        ),
    }

    resolved = _resolve(
        [
            {
                "id": "input",
                "path": "keymasq:2dc8:3106",
                "type": DeviceType.OTHER.value,
                "phys": "bluetooth/input0",
                "capabilities": ["btn_south"],
            }
        ],
        devices,
    )

    assert resolved == []


def test_phys_match_breaks_type_and_capability_ties() -> None:
    devices = {
        "/dev/input/event2": _FakeDevice(
            "/dev/input/event2",
            phys="bluetooth/input1",
        ),
        "/dev/input/event9": _FakeDevice(
            "/dev/input/event9",
            phys="bluetooth/input0",
        ),
    }

    resolved = _resolve(
        [
            {
                "id": "gamepad",
                "path": "keymasq:2dc8:3106",
                "type": "gamepad",
                "phys": "bluetooth/input0",
            }
        ],
        devices,
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event9"]


def test_equal_candidates_pick_deterministic_first_and_do_not_duplicate() -> None:
    devices = {
        "/dev/input/event9": _FakeDevice("/dev/input/event9"),
        "/dev/input/event2": _FakeDevice("/dev/input/event2"),
    }

    resolved = _resolve(
        [
            {"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"},
            {"id": "gamepad_2", "path": "keymasq:2dc8:3106", "type": "gamepad"},
        ],
        devices,
    )

    assert [interface.path for interface in resolved] == [
        "/dev/input/event2",
        "/dev/input/event9",
    ]


def test_logical_gamepad_matching_keeps_unnumbered_single_instance() -> None:
    devices = {
        "/dev/input/event2": _FakeDevice("/dev/input/event2", phys="usb-a/input0"),
        "/dev/input/event9": _FakeDevice("/dev/input/event9", phys="usb-b/input0"),
    }

    resolved = _resolve(
        [
            {
                "id": "gamepad",
                "path": "keymasq:2dc8:3106",
                "type": "gamepad",
                "phys": "usb-a/input0",
                "capabilities": ["btn_south", "abs_x", "abs_y"],
            }
        ],
        devices,
        hardware_id="2dc8:3106",
        match_model_gamepads=True,
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event2"]
    assert resolved[0].configured_path == "keymasq:2dc8:3106"


def test_logical_gamepad_matching_numbered_id_uses_first_unclaimed_candidate() -> None:
    devices = {
        "/dev/input/event2": _FakeDevice("/dev/input/event2", phys="usb-a/input0"),
        "/dev/input/event9": _FakeDevice("/dev/input/event9", phys="usb-b/input0"),
    }

    resolved = _resolve(
        [
            {
                "id": "gamepad",
                "path": "keymasq:2dc8:3106",
                "type": "gamepad",
                "phys": "usb-a/input0",
                "capabilities": ["btn_south", "abs_x", "abs_y"],
            }
        ],
        devices,
        hardware_id="2dc8:3106@2",
        match_model_gamepads=True,
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event2"]


def test_logical_gamepad_matching_numbered_id_does_not_require_instance_count() -> None:
    devices = {
        "/dev/input/event2": _FakeDevice("/dev/input/event2", phys="usb-a/input0"),
    }

    resolved = _resolve(
        [
            {
                "id": "gamepad",
                "path": "keymasq:2dc8:3106",
                "type": "gamepad",
                "phys": "usb-a/input0",
                "capabilities": ["btn_south", "abs_x", "abs_y"],
            }
        ],
        devices,
        hardware_id="2dc8:3106@2",
        match_model_gamepads=True,
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event2"]


def test_logical_gamepad_matching_numbered_id_skips_claimed_candidates() -> None:
    devices = {
        "/dev/input/event2": _FakeDevice("/dev/input/event2", phys="usb-a/input0"),
        "/dev/input/event9": _FakeDevice("/dev/input/event9", phys="usb-b/input0"),
    }

    resolved = _resolve(
        [
            {
                "id": "gamepad",
                "path": "keymasq:2dc8:3106",
                "type": "gamepad",
                "phys": "usb-a/input0",
                "capabilities": ["btn_south", "abs_x", "abs_y"],
            }
        ],
        devices,
        hardware_id="2dc8:3106@2",
        excluded_paths={"/dev/input/event2"},
        match_model_gamepads=True,
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event9"]


def test_logical_gamepad_matching_uses_stable_path_order_for_candidates() -> None:
    devices = {
        "/dev/input/event10": _FakeDevice("/dev/input/event10", phys="usb-b/input0"),
        "/dev/input/event9": _FakeDevice("/dev/input/event9", phys="usb-a/input0"),
    }
    stable_paths = {
        "/dev/input/event10": "/dev/input/by-path/test-if02-event-joystick",
        "/dev/input/event9": "/dev/input/by-path/test-event-joystick",
    }

    resolved = _resolve(
        [
            {
                "id": "gamepad",
                "path": "keymasq:2dc8:3106",
                "type": "gamepad",
                "capabilities": ["btn_south", "abs_x", "abs_y"],
            }
        ],
        devices,
        hardware_id="2dc8:3106@2",
        resolve_stable_path_fn=lambda path: stable_paths[path],
        match_model_gamepads=True,
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event9"]


def test_logical_gamepad_matching_reuses_stable_path_for_candidate_checks() -> None:
    devices = {
        "/dev/input/event10": _FakeDevice("/dev/input/event10", phys="usb-b/input0"),
        "/dev/input/event9": _FakeDevice("/dev/input/event9", phys="usb-a/input0"),
    }
    stable_paths = {
        "/dev/input/event10": "/dev/input/by-path/current-pad",
        "/dev/input/event9": "/dev/input/by-path/free-pad",
    }
    stable_path_calls: list[str] = []

    def resolve_stable_path(path: str) -> str:
        stable_path_calls.append(path)
        return stable_paths[path]

    resolved = _resolve(
        [
            {
                "id": "gamepad",
                "path": "keymasq:2dc8:3106",
                "type": "gamepad",
                "capabilities": ["btn_south", "abs_x", "abs_y"],
            }
        ],
        devices,
        hardware_id="2dc8:3106@2",
        preferred_paths={"/dev/input/by-path/current-pad"},
        resolve_stable_path_fn=resolve_stable_path,
        match_model_gamepads=True,
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event10"]
    assert stable_path_calls == ["/dev/input/event10", "/dev/input/event9"]


def test_keymasq_path_skips_excluded_candidate() -> None:
    devices = {
        "/dev/input/event9": _FakeDevice("/dev/input/event9"),
        "/dev/input/event2": _FakeDevice("/dev/input/event2"),
    }

    resolved = _resolve(
        [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}],
        devices,
        excluded_paths={"/dev/input/event2"},
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event9"]


def test_keymasq_path_skips_excluded_stable_path_alias() -> None:
    devices = {
        "/dev/input/event9": _FakeDevice("/dev/input/event9"),
        "/dev/input/event2": _FakeDevice("/dev/input/event2"),
    }
    stable_paths = {
        "/dev/input/event2": "/dev/input/by-id/claimed-pad",
        "/dev/input/event9": "/dev/input/event9",
    }

    resolved = _resolve(
        [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}],
        devices,
        excluded_paths={"/dev/input/by-id/claimed-pad"},
        resolve_stable_path_fn=lambda path: stable_paths[path],
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event9"]


def test_numbered_hardware_id_uses_first_keymasq_candidate() -> None:
    devices = {
        "/dev/input/event9": _FakeDevice("/dev/input/event9"),
        "/dev/input/event2": _FakeDevice("/dev/input/event2"),
    }

    resolved = _resolve(
        [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}],
        devices,
        hardware_id="2dc8:3106@2",
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event2"]


def test_numbered_hardware_id_skips_claimed_keymasq_candidate() -> None:
    devices = {
        "/dev/input/event9": _FakeDevice("/dev/input/event9"),
        "/dev/input/event2": _FakeDevice("/dev/input/event2"),
    }

    resolved = _resolve(
        [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}],
        devices,
        hardware_id="2dc8:3106@2",
        excluded_paths={"/dev/input/event2"},
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event9"]


def test_numbered_hardware_id_prefers_existing_keymasq_candidate() -> None:
    devices = {
        "/dev/input/event9": _FakeDevice("/dev/input/event9"),
        "/dev/input/event2": _FakeDevice("/dev/input/event2"),
    }

    resolved = _resolve(
        [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}],
        devices,
        hardware_id="2dc8:3106@7",
        preferred_paths={"/dev/input/event9"},
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event9"]


def test_numbered_hardware_id_prefers_existing_stable_path_alias() -> None:
    devices = {
        "/dev/input/event9": _FakeDevice("/dev/input/event9"),
        "/dev/input/event2": _FakeDevice("/dev/input/event2"),
    }
    stable_paths = {
        "/dev/input/event2": "/dev/input/by-id/free-pad",
        "/dev/input/event9": "/dev/input/by-id/current-pad",
    }

    resolved = _resolve(
        [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}],
        devices,
        hardware_id="2dc8:3106@7",
        preferred_paths={"/dev/input/by-id/current-pad"},
        resolve_stable_path_fn=lambda path: stable_paths[path],
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event9"]


def test_numbered_hardware_id_suffix_does_not_require_instance_count() -> None:
    devices = {
        "/dev/input/event2": _FakeDevice("/dev/input/event2"),
    }

    resolved = _resolve(
        [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}],
        devices,
        hardware_id="2dc8:3106@8",
    )

    assert [interface.path for interface in resolved] == ["/dev/input/event2"]


def test_unresolved_keymasq_path_returns_no_interface() -> None:
    resolved = _resolve(
        [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}],
        {"/dev/input/event1": _FakeDevice("/dev/input/event1", vendor=0x9999)},
    )

    assert resolved == []
