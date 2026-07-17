import asyncio
import errno
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import evdev
import pytest

from keymasq.common.model.core import DeviceType
from keymasq.common.types import JsonObject
from keymasq.keymasqd.runtime import device_path_resolver, outputs
from keymasq.keymasqd.runtime.grab import acquisition
from keymasq.keymasqd.runtime.grab.acquisition import finalize_grab
from keymasq.keymasqd.runtime.grab.planning import build_grab_plan, device_has_mapped_buttons
from keymasq.keymasqd.runtime.grab.recovery import rollback_failed_grab_report
from keymasq.keymasqd.runtime.grab.state import (
    GrabAcquisitionState,
    GrabDeviceDeps,
    GrabPlan,
    GrabRequest,
)


def _manager(
    *,
    grabbed_devices: dict[str, list[object]] | None = None,
    desired_paths: dict[str, set[str]] | None = None,
    desired_grabs: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        grabbed_devices=grabbed_devices or {},
        grab_state=SimpleNamespace(
            desired_paths=desired_paths or {},
            desired_grabs=desired_grabs or {},
            pending_interface_release={},
            pending_hardware_release={},
            release_grace_s=0.01,
        ),
    )


def _deps(
    *,
    resolve_stable_path_fn=lambda path: path,
) -> GrabDeviceDeps:
    return GrabDeviceDeps(
        desired_grab_config_cls=lambda **kwargs: SimpleNamespace(**kwargs),
        clear_device_path_cache_fn=lambda: None,
        resolve_stable_path_fn=resolve_stable_path_fn,
        device_path_resolver_deps=device_path_resolver.DevicePathResolverDeps(
            device_paths_fn=lambda: [],
            device_input_fn=lambda _path: SimpleNamespace(),
            detect_input_classes_fn=lambda _device: [],
            primary_input_class_fn=lambda _types: DeviceType.KEYBOARD,
        ),
        grabbed_device_cls=lambda **kwargs: SimpleNamespace(**kwargs),
        get_interface_id_fn=lambda _path: "",
        str_value_fn=str,
        int_value_fn=int,
        fire_and_observe_fn=lambda coro, _label: asyncio.ensure_future(coro),
        errno_mod=errno,
    )


def _request(
    *,
    hardware_id: str = "2dc8:3106",
    evdev_paths: list[str] | None = None,
    button_map: dict[str, str] | None = None,
    button_codes: dict[str, int] | None = None,
    button_values: dict[str, int] | None = None,
    analog_inputs: dict[str, object] | None = None,
    force_grab_unmapped: bool = False,
    evdev_interfaces: list[JsonObject] | None = None,
    update_desired: bool = False,
) -> GrabRequest:
    return GrabRequest(
        hardware_id=hardware_id,
        evdev_paths=evdev_paths or [],
        button_map=button_map or {},
        button_codes=button_codes,
        button_values=button_values,
        analog_inputs=analog_inputs,
        force_grab_unmapped=force_grab_unmapped,
        evdev_interfaces=evdev_interfaces,
        update_desired=update_desired,
    )


def _plan(
    *,
    hardware_id: str = "2dc8:3106",
    raw_interfaces: list[JsonObject] | None = None,
    evdev_interfaces_provided: bool = False,
    requested_paths: set[str] | None = None,
    mapped_evdev_names: set[str] | None = None,
    mapped_bindings: set[tuple[int, int]] | None = None,
    analog_inputs: dict[str, object] | None = None,
    existing_devices: list[object] | None = None,
    previous_desired_paths: set[str] | None = None,
    previous_desired_config: object | None = None,
    requests_gamepad_source_hiding: bool = False,
) -> GrabPlan:
    return GrabPlan(
        hardware_id=hardware_id,
        raw_interfaces=raw_interfaces or [],
        evdev_interfaces_provided=evdev_interfaces_provided,
        resolved_interfaces=[],
        requested_paths=requested_paths or set(),
        requested_claim_paths=set(),
        resolved_by_claim_path={},
        desired_paths=set(),
        mapped_evdev_names=mapped_evdev_names or set(),
        resolved_button_codes={},
        resolved_button_values={},
        button_mapped_bindings=set(),
        mapped_bindings=mapped_bindings or set(),
        analog_inputs=analog_inputs or {},
        existing_devices=existing_devices or [],
        existing_by_claim_path={},
        previous_desired_paths=previous_desired_paths,
        previous_desired_config=previous_desired_config,
        requests_gamepad_source_hiding=requests_gamepad_source_hiding,
    )


def test_build_grab_plan_computes_paths_bindings_aliases_and_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_config = object()
    first_existing = SimpleNamespace(
        path="/dev/input/event4",
        stable_path="",
        resolved_event_path="",
    )
    second_existing = SimpleNamespace(
        path="/dev/input/event5",
        stable_path="",
        resolved_event_path="",
    )
    manager = _manager(
        grabbed_devices={
            "2dc8:3106": [first_existing, second_existing],
            "ffff:ffff": [SimpleNamespace(path="/dev/input/event9")],
        },
        desired_paths={"2dc8:3106": {"old-path"}},
        desired_grabs={"2dc8:3106": old_config},
    )
    stable_paths = {
        "/dev/input/event2": "/dev/input/by-id/kbd",
        "/dev/input/event3": "/dev/input/by-id/stick",
        "/dev/input/event4": "/dev/input/by-id/shared",
        "/dev/input/event5": "/dev/input/by-id/shared",
        "/dev/input/event9": "/dev/input/by-id/other",
    }

    def resolve_stable_path(path: str) -> str:
        return stable_paths.get(path, path)

    resolved = [
        device_path_resolver.ResolvedInterface(
            path="/dev/input/event2",
            configured_path="/dev/input/event2",
            interface_id="kbd",
            device_type=DeviceType.KEYBOARD,
            capabilities=["key_a"],
        ),
        device_path_resolver.ResolvedInterface(
            path="/dev/input/event3",
            configured_path="/dev/input/event3",
            interface_id="stick",
            device_type=DeviceType.GAMEPAD,
            capabilities=["abs_x"],
        ),
    ]

    def resolve_evdev_interfaces(_interfaces, **kwargs):
        assert "/dev/input/by-id/other" in kwargs["excluded_paths"]
        assert "/dev/input/by-id/shared" in kwargs["preferred_paths"]
        return resolved

    monkeypatch.setattr(
        device_path_resolver,
        "resolve_evdev_interfaces",
        resolve_evdev_interfaces,
    )
    raw_interfaces = [
        {"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"},
        {"id": "kbd", "path": "/dev/input/event2", "type": "keyboard"},
    ]
    analog_inputs = {
        "stick": {
            "source": "stick",
            "type": "stick",
            "axes": [{"role": "x", "evdev_code": evdev.ecodes.ABS_X}],
        }
    }

    plan = build_grab_plan(
        manager,
        _request(
            evdev_interfaces=raw_interfaces,
            button_map={
                "fire": "key_a",
                "missing": "not_an_evdev_name",
                "south": "btn_south",
            },
            button_codes={
                "fire": evdev.ecodes.KEY_A,
                "missing": 99,
                "south": evdev.ecodes.BTN_SOUTH,
            },
            button_values={"south": 1},
            analog_inputs=analog_inputs,
        ),
        _deps(resolve_stable_path_fn=resolve_stable_path),
    )

    assert plan.desired_paths == {
        "keymasq:2dc8:3106",
        "/dev/input/event2",
        "/dev/input/by-id/kbd",
        "/dev/input/by-id/stick",
    }
    assert plan.requested_paths == {
        "/dev/input/by-id/kbd",
        "/dev/input/by-id/stick",
    }
    assert plan.requested_claim_paths >= {
        "/dev/input/event2",
        "/dev/input/by-id/kbd",
    }
    assert plan.resolved_by_claim_path["/dev/input/by-id/kbd"] is resolved[0]
    assert plan.existing_by_claim_path["/dev/input/by-id/shared"] is first_existing
    assert plan.previous_desired_paths == {"old-path"}
    manager.grab_state.desired_paths["2dc8:3106"].add("changed")
    assert plan.previous_desired_paths == {"old-path"}
    assert plan.previous_desired_config is old_config
    assert plan.button_mapped_bindings == {
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A),
        (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH),
    }
    assert plan.mapped_bindings == plan.button_mapped_bindings | {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X)
    }
    assert plan.requests_gamepad_source_hiding is True


@pytest.mark.parametrize(
    ("interfaces", "expected"),
    [
        ([{"path": "keymasq:2dc8:3106", "type": "gamepad"}], True),
        ([{"path": "/dev/input/event2", "type": "gamepad"}], False),
        ([{"path": "keymasq:2dc8:3106", "type": "keyboard"}], False),
        ([], False),
    ],
)
def test_build_grab_plan_source_hiding_requires_keymasq_gamepad(
    monkeypatch: pytest.MonkeyPatch,
    interfaces: list[JsonObject],
    expected: bool,
) -> None:
    monkeypatch.setattr(
        device_path_resolver,
        "resolve_evdev_interfaces",
        lambda *_args, **_kwargs: [],
    )

    plan = build_grab_plan(
        _manager(),
        _request(evdev_interfaces=interfaces),
        _deps(),
    )

    assert plan.requests_gamepad_source_hiding is expected


@pytest.mark.parametrize(
    ("caps", "mapped_names", "mapped_bindings", "expected"),
    [
        ({evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A]}, set(), {(evdev.ecodes.EV_KEY, 30)}, True),
        (
            {evdev.ecodes.EV_ABS: [(evdev.ecodes.ABS_X, object())]},
            set(),
            {(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X)},
            True,
        ),
        (
            {evdev.ecodes.EV_KEY: ["KEY_A"]},
            {"key_a"},
            {(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A)},
            False,
        ),
        (
            {evdev.ecodes.EV_SYN: [evdev.ecodes.KEY_A]},
            {"key_a"},
            {(evdev.ecodes.EV_SYN, evdev.ecodes.KEY_A)},
            False,
        ),
        ({evdev.ecodes.EV_REL: [7]}, set(), {(evdev.ecodes.EV_KEY, 7)}, False),
        ({evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A]}, {"key_a"}, set(), True),
        (
            {evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_B]},
            set(),
            {(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B)},
            True,
        ),
    ],
)
def test_device_has_mapped_buttons_matrix(
    caps: dict[int, list[object]],
    mapped_names: set[str],
    mapped_bindings: set[tuple[int, int]],
    expected: bool,
) -> None:
    assert (
        device_has_mapped_buttons(
            caps,
            mapped_names,
            mapped_bindings,
            evdev_mod=evdev,
        )
        is expected
    )


@pytest.mark.asyncio
async def test_finalize_grab_waits_when_interfaces_requested_but_none_available() -> None:
    manager = _manager()
    result = await finalize_grab(
        manager,
        _request(update_desired=False),
        _plan(raw_interfaces=[{"path": "keymasq:2dc8:3106"}]),
        _deps(),
        GrabAcquisitionState(devices=[]),
    )

    assert result["waiting_for_device"] is True
    assert result["grabbed_count"] == 0
    assert manager.grabbed_devices == {}


@pytest.mark.asyncio
async def test_finalize_grab_raises_no_match_and_destroys_created_uinputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroy_global_uinputs = Mock()
    monkeypatch.setattr(outputs, "destroy_global_uinputs", destroy_global_uinputs)
    state = GrabAcquisitionState(
        devices=[],
        available_count=1,
        created_global_uinputs=True,
    )

    with pytest.raises(ValueError) as excinfo:
        await finalize_grab(
            _manager(),
            _request(update_desired=False),
            _plan(
                requested_paths={"/dev/input/event2"},
                mapped_evdev_names={"key_a"},
                mapped_bindings={(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A)},
            ),
            _deps(),
            state,
        )

    assert "No interfaces for 2dc8:3106 matched mapped buttons" in str(excinfo.value)
    assert "paths=1" in str(excinfo.value)
    assert "mapped_names=1" in str(excinfo.value)
    assert "mapped_bindings=1" in str(excinfo.value)
    destroy_global_uinputs.assert_called_once()


@pytest.mark.asyncio
async def test_failed_multi_interface_grab_does_not_update_existing_device_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = SimpleNamespace(path="/dev/input/event1")
    manager = _manager(grabbed_devices={"2dc8:3106": [existing]})
    plan = _plan(
        requested_paths={"/dev/input/event1", "/dev/input/event2"},
        existing_devices=[existing],
    )
    update_existing_devices = Mock()
    monkeypatch.setattr(acquisition.device_path_resolver, "clear_cached_devices", Mock())
    monkeypatch.setattr(acquisition, "build_grab_plan", Mock(return_value=plan))
    monkeypatch.setattr(acquisition, "log_grab_request", Mock())
    monkeypatch.setattr(acquisition, "update_existing_devices", update_existing_devices)
    monkeypatch.setattr(acquisition, "reconcile_existing_interface_releases", Mock())
    monkeypatch.setattr(acquisition, "build_runtime_callbacks", Mock(return_value=object()))
    grab_one_interface = AsyncMock(
        side_effect=[None, RuntimeError("second interface failed")]
    )
    monkeypatch.setattr(
        acquisition,
        "grab_one_interface",
        grab_one_interface,
    )

    with pytest.raises(RuntimeError, match="second interface failed"):
        await acquisition.grab_device_unlocked(
            manager,
            _request(),
            _deps(),
        )

    update_existing_devices.assert_not_called()
    assert grab_one_interface.await_count == 2


class _FakeTask:
    def __init__(self) -> None:
        self.cancelled = False

    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        self.cancelled = True


class _ReleasedDevice:
    def __init__(self, path: str) -> None:
        self.path = path
        self.stable_path = path
        self.resolved_event_path = path
        self.release_count = 0

    async def release(self) -> None:
        self.release_count += 1


class _ExistingDevice(_ReleasedDevice):
    async def release(self) -> None:
        raise AssertionError("existing devices must not be released during rollback")


class _FailingReleaseDevice(_ReleasedDevice):
    async def release(self) -> None:
        self.release_count += 1
        raise RuntimeError("release failed")


@pytest.mark.asyncio
async def test_rollback_failed_grab_restores_existing_devices_and_desired_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _ExistingDevice("/dev/input/event1")
    first_new = _ReleasedDevice("/dev/input/event2")
    old_config = object()
    new_config = object()
    task = _FakeTask()
    other_task = _FakeTask()
    manager = _manager(
        grabbed_devices={"2dc8:3106": [existing, first_new]},
        desired_paths={"2dc8:3106": {"new-path"}},
        desired_grabs={"2dc8:3106": new_config},
    )
    manager.grab_state.pending_interface_release[("2dc8:3106", "/dev/input/event1")] = task
    manager.grab_state.pending_interface_release[("ffff:ffff", "/dev/input/event9")] = other_task
    destroy_global_uinputs = Mock()
    monkeypatch.setattr(outputs, "destroy_global_uinputs", destroy_global_uinputs)
    exc = RuntimeError("grab failed")

    reported = await rollback_failed_grab_report(
        manager,
        _request(update_desired=True),
        _plan(
            existing_devices=[existing],
            previous_desired_paths={"old-path"},
            previous_desired_config=old_config,
        ),
        GrabAcquisitionState(
            devices=[existing, first_new],
            grabbed_count=1,
            created_global_uinputs=True,
        ),
        "/dev/input/event3",
        exc,
    )

    assert reported.reported_exception is exc
    assert reported.cleanup_succeeded
    assert first_new.release_count == 1
    assert manager.grabbed_devices["2dc8:3106"] == [existing]
    assert manager.grab_state.desired_paths["2dc8:3106"] == {"old-path"}
    assert manager.grab_state.desired_grabs["2dc8:3106"] is old_config
    assert ("2dc8:3106", "/dev/input/event1") not in manager.grab_state.pending_interface_release
    assert (
        manager.grab_state.pending_interface_release[("ffff:ffff", "/dev/input/event9")]
        is other_task
    )
    assert task.cancelled is True
    assert other_task.cancelled is False
    destroy_global_uinputs.assert_called_once()


@pytest.mark.asyncio
async def test_rollback_failed_grab_restores_state_when_new_device_release_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _ExistingDevice("/dev/input/event1")
    failing_new = _FailingReleaseDevice("/dev/input/event2")
    old_config = object()
    task = _FakeTask()
    manager = _manager(
        grabbed_devices={"2dc8:3106": [existing, failing_new]},
        desired_paths={"2dc8:3106": {"new-path"}},
        desired_grabs={"2dc8:3106": object()},
    )
    manager.grab_state.pending_interface_release[("2dc8:3106", "/dev/input/event1")] = task
    destroy_global_uinputs = Mock()
    monkeypatch.setattr(outputs, "destroy_global_uinputs", destroy_global_uinputs)
    exc = RuntimeError("grab failed")

    reported = await rollback_failed_grab_report(
        manager,
        _request(update_desired=True),
        _plan(
            existing_devices=[existing],
            previous_desired_paths={"old-path"},
            previous_desired_config=old_config,
        ),
        GrabAcquisitionState(
            devices=[existing, failing_new],
            created_global_uinputs=True,
        ),
        "/dev/input/event3",
        exc,
    )

    assert reported.reported_exception is exc
    assert reported.failed_release_paths == ("/dev/input/event2",)
    assert failing_new.release_count == 1
    assert manager.grabbed_devices["2dc8:3106"] == [existing]
    assert manager.grab_state.desired_paths["2dc8:3106"] == {"old-path"}
    assert manager.grab_state.desired_grabs["2dc8:3106"] is old_config
    assert ("2dc8:3106", "/dev/input/event1") not in manager.grab_state.pending_interface_release
    assert task.cancelled is True
    destroy_global_uinputs.assert_called_once()
