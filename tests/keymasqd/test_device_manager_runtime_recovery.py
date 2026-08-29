import asyncio
import errno
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import evdev
import pytest
from evdev.uinput import UInputError

from keymasq.common.ipc import CommandType
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType, DeviceType, SuperkeyMode
from keymasq.keymasqd import device_manager
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.permission_hints import UINPUT_PERMISSION_HINT
from keymasq.keymasqd.runtime import action_parser, adapters, outputs, topology
from keymasq.keymasqd.runtime.combo import events, lifecycle
from keymasq.keymasqd.runtime.grab import acquisition, planning, release
from keymasq.keymasqd.runtime.grabbed_device import (
    actions as device_actions,
)
from keymasq.keymasqd.runtime.grabbed_device import (
    device as grabbed_device,
)
from keymasq.keymasqd.runtime.grabbed_device import (
    outputs as device_outputs,
)
from keymasq.keymasqd.runtime.grabbed_device.event import pipeline
from keymasq.keymasqd.runtime.grabbed_device.types import EventProcessingDeps
from keymasq.keymasqd.superkey_state import SuperkeyActionData, SuperkeyConfig
from tests.keymasqd.device_manager_support import (
    FakeUInput,
    combo_runtime_deps,
    make_grabbed_device,
)


class TestEventLoopRecovery:
    @pytest.mark.asyncio
    async def test_cancelled_inflight_event_does_not_stop_loop_after_resume(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = make_grabbed_device(monkeypatch, running=True)
        first_started = asyncio.Event()
        first_cancelled = asyncio.Event()
        release_second = asyncio.Event()
        processed_values: list[int] = []

        class _FakeInputDevice:
            async def async_read_loop(self):
                yield SimpleNamespace(type=evdev.ecodes.EV_KEY, code=30, value=1)
                await release_second.wait()
                yield SimpleNamespace(type=evdev.ecodes.EV_KEY, code=30, value=0)

        async def fake_process_event(_device, event, **_kwargs) -> None:
            if int(event.value) == 1:
                first_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    first_cancelled.set()
                return
            processed_values.append(int(event.value))

        monkeypatch.setattr(pipeline, "process_event", fake_process_event)
        device.device = _FakeInputDevice()  # type: ignore[assignment]
        loop_task = asyncio.create_task(
            pipeline.event_loop(
                device,
                asyncio_mod=adapters.ASYNCIO_RUNTIME,
                log=grabbed_device.log,
            )
        )
        await first_started.wait()

        device.input_suspended = True
        await device.cancel_inflight_actions()
        device.input_suspended = False
        release_second.set()
        await loop_task

        assert first_cancelled.is_set()
        assert processed_values == [0]

    @pytest.mark.asyncio
    async def test_suspended_event_loop_discards_input(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = make_grabbed_device(monkeypatch, running=True)
        device.input_suspended = True

        class _FakeInputDevice:
            async def async_read_loop(self):
                yield SimpleNamespace(type=evdev.ecodes.EV_KEY, code=30, value=1)

        process_event = AsyncMock()
        monkeypatch.setattr(pipeline, "process_event", process_event)
        device.device = _FakeInputDevice()  # type: ignore[assignment]

        await pipeline.event_loop(
            device, asyncio_mod=adapters.ASYNCIO_RUNTIME, log=grabbed_device.log
        )

        process_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_event_processing_dependencies_are_reused_per_loop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = make_grabbed_device(monkeypatch, running=True)
        events = [
            SimpleNamespace(type=evdev.ecodes.EV_SYN, code=0, value=0),
            SimpleNamespace(type=evdev.ecodes.EV_SYN, code=0, value=0),
        ]

        class _FakeInputDevice:
            async def async_read_loop(self):
                for event in events:
                    yield event

        original_build_event_processing_deps = pipeline.build_event_processing_deps
        built_deps: list[EventProcessingDeps] = []
        processed_deps: list[EventProcessingDeps] = []

        def fake_build_event_processing_deps(
            *,
            log: logging.Logger,
            fire_and_observe_fn=pipeline.fire_and_observe,
        ) -> EventProcessingDeps:
            deps = original_build_event_processing_deps(
                log=log,
                fire_and_observe_fn=fire_and_observe_fn,
            )
            built_deps.append(deps)
            return deps

        async def fake_process_event(
            _device,
            _event,
            *,
            deps: EventProcessingDeps,
        ) -> None:
            processed_deps.append(deps)

        monkeypatch.setattr(
            pipeline,
            "build_event_processing_deps",
            fake_build_event_processing_deps,
        )
        monkeypatch.setattr(pipeline, "process_event", fake_process_event)
        device.device = _FakeInputDevice()  # type: ignore[assignment]

        await pipeline.event_loop(
            device, asyncio_mod=adapters.ASYNCIO_RUNTIME, log=grabbed_device.log
        )

        assert len(built_deps) == 1
        assert processed_deps == [built_deps[0], built_deps[0]]

    @pytest.mark.asyncio
    async def test_event_processing_error_releases_held_output_before_backoff(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_uinput = FakeUInput()
        device = make_grabbed_device(
            monkeypatch,
            button_map={"key_f5": "key_f5"},
            mapping={
                "key_f5": MappingAction(
                    action_type=ActionType.KEYBOARD,
                    target="key_a",
                )
            },
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=fake_uinput,
            running=True,
        )

        class _FakeInputDevice:
            async def async_read_loop(self):
                yield SimpleNamespace(
                    type=evdev.ecodes.EV_KEY,
                    code=evdev.ecodes.KEY_F5,
                    value=1,
                )

        sleep_calls: list[float] = []
        original_execute_action = device_actions.execute_action

        async def fail_after_press(_device, action, event, event_name, **_kwargs):
            await original_execute_action(
                _device,
                action,
                event,
                event_name,
                deps=pipeline.build_action_execution_deps(
                    fire_and_observe_fn=lambda coro, _label: asyncio.create_task(coro)
                ),
            )
            raise RuntimeError("boom")

        async def fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr(device_actions, "execute_action", fail_after_press)
        monkeypatch.setattr(grabbed_device.asyncio, "sleep", fake_sleep)

        device.device = _FakeInputDevice()  # type: ignore[assignment]

        await pipeline.event_loop(
            device, asyncio_mod=adapters.ASYNCIO_RUNTIME, log=grabbed_device.log
        )

        assert sleep_calls == [0.01]
        assert fake_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
        assert device.state.held_output_keys["keyboard"] == set()
        assert device.state.held_source_actions == {}

    @pytest.mark.asyncio
    async def test_device_read_error_releases_disconnected_interface(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        disconnect = AsyncMock()
        device = make_grabbed_device(
            monkeypatch,
            path="/dev/input/event10",
            hardware_id="cafe:0002",
            runtime_disconnect_callback=disconnect,
            running=True,
        )

        class _FakeInputDevice:
            async def async_read_loop(self):
                raise OSError(errno.ENODEV, "No such device")
                yield

        device.device = _FakeInputDevice()  # type: ignore[assignment]

        await pipeline.event_loop(
            device, asyncio_mod=adapters.ASYNCIO_RUNTIME, log=grabbed_device.log
        )

        disconnect.assert_awaited_once_with("cafe:0002", "/dev/input/event10")

    @pytest.mark.asyncio
    async def test_release_tolerates_current_event_loop_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = make_grabbed_device(monkeypatch, running=True)
        device.task = asyncio.current_task()  # type: ignore[assignment]

        await device.release()

        assert device.task is None


class TestRuntimeFailureCleanup:
    @pytest.mark.asyncio
    async def test_event_processing_error_clears_scoped_runtime_and_releases_outputs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cleanup = AsyncMock()
        keyboard_uinput = FakeUInput()
        gamepad_uinput = FakeUInput()
        device = make_grabbed_device(
            monkeypatch,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=keyboard_uinput,
            gamepad_uinput=gamepad_uinput,
            runtime_cleanup_callback=cleanup,
            running=True,
        )
        device_outputs.write_key(
            device,
            keyboard_uinput,  # type: ignore[arg-type]
            evdev.ecodes.KEY_A,
            1,
            evdev_mod=evdev,
            uinput_writer=adapters.identity_uinput_writer,
        )

        await pipeline.recover_from_event_processing_error(device)

        cleanup.assert_awaited_once_with("1234:5678", "kbd")
        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
        assert gamepad_uinput.writes[-2:] == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ, 0),
        ]
        assert device.state.held_output_keys["keyboard"] == set()


class TestSuspendCleanup:
    @pytest.mark.asyncio
    async def test_cancel_inflight_actions_bounds_uncooperative_tasks(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = make_grabbed_device(monkeypatch, running=True)
        started = asyncio.Event()
        release = asyncio.Event()

        async def ignore_cancellation() -> None:
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        task = asyncio.create_task(ignore_cancellation())
        device.background_tasks.add(task)
        await started.wait()

        await device.cancel_inflight_actions(timeout_s=0)

        assert task.done() is False
        assert task in device.background_tasks
        release.set()
        await task
        await asyncio.sleep(0)
        assert task not in device.background_tasks

    @pytest.mark.asyncio
    async def test_cleanup_gates_input_before_waiting_for_recording_start(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        device = make_grabbed_device(monkeypatch, running=True)
        manager.grabbed_devices["1234:5678"] = [device]
        start_entered = asyncio.Event()
        finish_start = asyncio.Event()
        recording_manager = SimpleNamespace(is_recording=False)

        async def start(*_args, **_kwargs) -> dict[str, object]:
            start_entered.set()
            await finish_start.wait()
            recording_manager.is_recording = True
            return {"status": "ok"}

        async def abort() -> None:
            recording_manager.is_recording = False

        recording_manager.start = AsyncMock(side_effect=start)
        recording_manager.abort = AsyncMock(side_effect=abort)
        manager.recording_manager = recording_manager  # type: ignore[assignment]

        start_task = asyncio.create_task(manager.start_recording([]))
        await start_entered.wait()
        prepare_task = asyncio.create_task(manager.prepare_for_sleep())
        await asyncio.sleep(0)

        assert manager.sleep_preparing is True
        assert device.input_suspended is True
        finish_start.set()
        assert await start_task == {"status": "ok"}
        await prepare_task

        recording_manager.abort.assert_awaited_once_with()
        assert recording_manager.is_recording is False

        with pytest.raises(RuntimeError, match="preparing for sleep"):
            await manager.start_recording([])
        assert recording_manager.start.await_count == 1

    @pytest.mark.asyncio
    async def test_cleanup_flushes_open_passthrough_frame(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        passthrough = FakeUInput()
        passthrough.syn = Mock()  # type: ignore[method-assign]
        device = make_grabbed_device(
            monkeypatch,
            passthrough_uinput=passthrough,
            running=True,
        )
        device.state.passthrough_abs_neutral_values[evdev.ecodes.ABS_X] = 0
        device_outputs.passthrough(
            device,
            SimpleNamespace(
                type=evdev.ecodes.EV_ABS,
                code=evdev.ecodes.ABS_X,
                value=123,
            ),
            evdev_mod=evdev,
            uinput_writer=adapters.identity_uinput_writer,
            sync=False,
        )

        await device.neutralize_runtime_state()

        assert passthrough.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 123),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 0),
        ]
        assert passthrough.syn.call_count == 2
        assert not device_outputs.passthrough_frame_open(device, passthrough)
        assert device.state.held_output_abs["passthrough"] == set()

    @pytest.mark.asyncio
    async def test_cleanup_aborts_recording_after_input_is_suspended(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        device = make_grabbed_device(monkeypatch, running=True)
        manager.grabbed_devices["1234:5678"] = [device]
        broadcast = AsyncMock()
        manager.broadcast_callback = broadcast

        async def abort() -> None:
            assert device.input_suspended is True

        manager.recording_manager = SimpleNamespace(  # type: ignore[assignment]
            is_recording=True,
            abort=AsyncMock(side_effect=abort),
        )

        await manager.prepare_for_sleep()

        manager.recording_manager.abort.assert_awaited_once_with()
        broadcast.assert_awaited_once_with(
            CommandType.RECORDING_ABORTED,
            {"reason": "suspend"},
        )
        assert device.input_suspended is True

    @pytest.mark.asyncio
    async def test_cleanup_clears_active_repeat_actions(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        device = make_grabbed_device(monkeypatch, running=True)
        device.state.repeat_active_actions["key_a#repeat"] = MappingAction(
            action_type=ActionType.KEYBOARD,
            target="key_b",
        )
        manager.grabbed_devices["1234:5678"] = [device]

        await manager.prepare_for_sleep()

        assert device.state.repeat_active_actions == {}

    def test_passthrough_multitouch_cleanup_restores_source_slot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        passthrough = FakeUInput()
        device = make_grabbed_device(
            monkeypatch,
            passthrough_uinput=passthrough,
        )
        device.state.passthrough_mt_slot = 0
        device.state.passthrough_mt_uses_slots = True
        device.state.passthrough_mt_active_slots = {0, 2}

        device_outputs.neutralize_passthrough_abs(
            device,
            evdev_mod=evdev,
            uinput_writer=adapters.identity_uinput_writer,
        )

        assert passthrough.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_MT_SLOT, 0),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_MT_TRACKING_ID, -1),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_MT_SLOT, 2),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_MT_TRACKING_ID, -1),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_MT_SLOT, 0),
        ]
        assert device.state.passthrough_mt_active_slots == set()

    def test_passthrough_multitouch_cleanup_handles_slotless_type_a(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        passthrough = FakeUInput()
        device = make_grabbed_device(
            monkeypatch,
            passthrough_uinput=passthrough,
        )
        device.state.passthrough_mt_uses_slots = False
        device.state.passthrough_mt_active_slots = {0}

        device_outputs.neutralize_passthrough_abs(
            device,
            evdev_mod=evdev,
            uinput_writer=adapters.identity_uinput_writer,
        )

        assert passthrough.writes == [
            (
                evdev.ecodes.EV_ABS,
                evdev.ecodes.ABS_MT_TRACKING_ID,
                -1,
            ),
            (evdev.ecodes.EV_SYN, evdev.ecodes.SYN_MT_REPORT, 0),
        ]

    def test_passthrough_abs_neutral_ignores_displaced_grab_sample(self) -> None:
        displaced = SimpleNamespace(min=0, max=255, value=240)

        assert (
            grabbed_device._passthrough_abs_neutral_value(
                evdev.ecodes.ABS_X,
                displaced,
            )
            == 127
        )
        assert (
            grabbed_device._passthrough_abs_neutral_value(
                evdev.ecodes.ABS_Z,
                displaced,
            )
            == 0
        )
        assert (
            grabbed_device._passthrough_abs_neutral_value(
                evdev.ecodes.ABS_X,
                SimpleNamespace(min=-32768, max=32767, value=12000),
            )
            == 0
        )

    @pytest.mark.asyncio
    async def test_passthrough_abs_probes_run_off_event_loop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = make_grabbed_device(monkeypatch)
        source = SimpleNamespace(
            absinfo=Mock(return_value=SimpleNamespace(min=0, max=255, value=240))
        )
        device.device = source  # type: ignore[assignment]
        offloaded: list[tuple[object, tuple[object, ...]]] = []

        async def fake_to_thread(func, /, *args, **kwargs):
            assert kwargs == {}
            offloaded.append((func, args))
            return func(*args)

        monkeypatch.setattr(grabbed_device.asyncio, "to_thread", fake_to_thread)

        await device._refresh_passthrough_abs_neutral_values(
            {evdev.ecodes.EV_ABS: [evdev.ecodes.ABS_X]}
        )

        assert offloaded == [(source.absinfo, (evdev.ecodes.ABS_X,))]
        assert device.state.passthrough_abs_neutral_values[evdev.ecodes.ABS_X] == 127

    @pytest.mark.asyncio
    async def test_cleanup_drains_inflight_event_before_global_runtime(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        device = make_grabbed_device(monkeypatch, running=True)
        manager.grabbed_devices["1234:5678"] = [device]
        started = asyncio.Event()
        order: list[str] = []

        async def inflight_event() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                order.append("event_cancelled")

        async def cancel_macros() -> dict[str, object]:
            order.append("macro_cleanup")
            return {"cancelled": False}

        device.current_event_task = asyncio.create_task(inflight_event())
        monkeypatch.setattr(manager, "cancel_macro_playback", cancel_macros)
        await started.wait()

        await manager.prepare_for_sleep()

        assert order[:2] == ["event_cancelled", "macro_cleanup"]

    @pytest.mark.asyncio
    async def test_cleanup_cancels_detached_device_action_tasks(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = make_grabbed_device(monkeypatch, running=True)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def detached_action() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        device.fire_and_observe(detached_action(), "detached test action")
        await started.wait()

        await device.neutralize_runtime_state()

        assert cancelled.is_set()
        assert device.background_tasks == set()

    @pytest.mark.asyncio
    async def test_cleanup_cancels_detached_tap_natural_move(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def natural_mouse_mover(*_args) -> dict[str, object]:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return {"status": "ok"}

        device = make_grabbed_device(
            monkeypatch,
            running=True,
            natural_mouse_mover=natural_mouse_mover,
        )
        action = MappingAction(
            action_type=ActionType.MOUSE_MOVE_NATURAL_ABS,
            move_x=100,
            move_y=200,
            tap_enabled=True,
            tap_hold_ms=500,
        )
        await device_actions.execute_action(
            device,
            action,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=30, value=1),
            "key_a",
            deps=pipeline.build_action_execution_deps(
                fire_and_observe_fn=device.fire_and_observe,
            ),
        )
        await started.wait()

        await device.neutralize_runtime_state()

        assert cancelled.is_set()

    @pytest.mark.asyncio
    async def test_cleanup_preserves_conditional_profile_trackers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        device = make_grabbed_device(monkeypatch, running=True)
        manager.grabbed_devices["1234:5678"] = [device]
        await manager.track_profile_activation(
            "Temporary",
            "activation-1",
            "1234:5678:key_a",
            {"timeout_ms": 60_000},
        )

        try:
            await manager.prepare_for_sleep()

            assert "activation-1" in manager.profile_activation_tracker._trackers
        finally:
            manager.profile_activation_tracker.reset()

    @pytest.mark.asyncio
    async def test_cleanup_releases_outputs_without_dropping_grabs_or_mappings(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        keyboard_uinput = FakeUInput()
        device = make_grabbed_device(
            monkeypatch,
            hardware_id="cafe:0001",
            keyboard_uinput=keyboard_uinput,
            running=True,
        )
        device_outputs.write_key(
            device,
            keyboard_uinput,  # type: ignore[arg-type]
            evdev.ecodes.KEY_A,
            1,
            evdev_mod=evdev,
            uinput_writer=adapters.identity_uinput_writer,
        )
        manager = DeviceManager()
        manager.grabbed_devices["cafe:0001"] = [device]
        mapping = MappingAction(action_type=ActionType.KEYBOARD, target="key_b")
        manager.active_mappings["cafe:0001"] = {"key_a": mapping}
        grabbed_devices = manager.grabbed_devices
        active_mappings = manager.active_mappings

        await manager.prepare_for_sleep()

        assert manager.grabbed_devices is grabbed_devices
        assert manager.grabbed_devices["cafe:0001"] == [device]
        assert manager.active_mappings is active_mappings
        assert manager.active_mappings["cafe:0001"] == {"key_a": mapping}
        assert device.running is True
        assert device.input_suspended is True
        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]

        await manager.resume_from_sleep()

        assert device.input_suspended is False

    @pytest.mark.asyncio
    async def test_event_processing_error_resets_analog_controls(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = make_grabbed_device(
            monkeypatch,
            device_type=DeviceType.KEYBOARD,
        )
        reset_analog_controls = AsyncMock()
        monkeypatch.setattr(device, "reset_analog_controls", reset_analog_controls)

        await pipeline.recover_from_event_processing_error(device)

        reset_analog_controls.assert_awaited_once()


class TestDeviceManagerHelpers:
    def test_create_global_uinputs_uses_explicit_test_identities(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KEYMASQ_TEST_UINPUT", "1")
        manager = SimpleNamespace(
            output_state=SimpleNamespace(
                device_count=0,
                keyboard_uinput=None,
                mouse_uinput=None,
                gamepad_uinput=None,
            )
        )
        created: list[FakeUInput] = []

        def fake_uinput(**kwargs) -> FakeUInput:
            device = FakeUInput(**kwargs)
            created.append(device)
            return device

        outputs.create_global_uinputs(
            manager,
            evdev_mod=SimpleNamespace(
                ecodes=evdev.ecodes,
                UInput=fake_uinput,
                AbsInfo=evdev.AbsInfo,
            ),
            log=logging.getLogger("test"),
            uinput_writer=lambda device: device,
        )

        assert manager.output_state.device_count == 1
        assert len(created) == 3
        assert created[0].kwargs["name"] == "keymasq-test-keyboard"
        assert created[0].kwargs["vendor"] == 0x4B46
        assert created[0].kwargs["product"] == 0x1001
        keyboard_key_caps = set(created[0].kwargs["events"][evdev.ecodes.EV_KEY])
        expected_keyboard_keys = {
            int(code)
            for code in evdev.ecodes.KEY
            if evdev.ecodes.KEY_RESERVED < int(code) < evdev.ecodes.KEY_MAX
        }
        assert keyboard_key_caps == expected_keyboard_keys
        assert {
            evdev.ecodes.KEY_COMPOSE,
            evdev.ecodes.KEY_RO,
            evdev.ecodes.KEY_YEN,
            evdev.ecodes.KEY_KPEQUAL,
            evdev.ecodes.KEY_BRL_DOT1,
            evdev.ecodes.KEY_OK,
        } <= keyboard_key_caps
        assert keyboard_key_caps.isdisjoint(evdev.ecodes.BTN)
        assert evdev.ecodes.KEY_RESERVED not in keyboard_key_caps
        assert evdev.ecodes.KEY_MAX not in keyboard_key_caps
        assert evdev.ecodes.KEY_CNT not in keyboard_key_caps
        assert created[1].kwargs["name"] == "keymasq-test-mouse"
        assert created[1].kwargs["vendor"] == 0x4B46
        assert created[1].kwargs["product"] == 0x1002
        mouse_rel_caps = created[1].kwargs["events"][evdev.ecodes.EV_REL]
        assert evdev.ecodes.REL_WHEEL in mouse_rel_caps
        rel_wheel_hi_res = getattr(evdev.ecodes, "REL_WHEEL_HI_RES", None)
        if rel_wheel_hi_res is not None:
            assert int(rel_wheel_hi_res) in mouse_rel_caps
        assert created[2].kwargs["name"] == "keymasq-test-gamepad"
        assert created[2].kwargs["vendor"] == 0x4B46
        assert created[2].kwargs["product"] == 0x1003

    def test_create_global_uinputs_permission_error_mentions_uinput(self) -> None:
        manager = SimpleNamespace(
            output_state=SimpleNamespace(
                device_count=0,
                keyboard_uinput=None,
                mouse_uinput=None,
                gamepad_uinput=None,
            )
        )

        def fail_uinput(**_kwargs) -> FakeUInput:
            raise PermissionError(errno.EACCES, "denied")

        with pytest.raises(PermissionError) as excinfo:
            outputs.create_global_uinputs(
                manager,
                evdev_mod=SimpleNamespace(
                    ecodes=evdev.ecodes,
                    UInput=fail_uinput,
                    AbsInfo=evdev.AbsInfo,
                ),
                log=logging.getLogger("test"),
                uinput_writer=lambda device: device,
            )

        message = str(excinfo.value)
        assert "keyboard uinput device" in message
        assert UINPUT_PERMISSION_HINT in message
        assert manager.output_state.device_count == 0

    def test_create_global_uinputs_uinput_error_mentions_uinput(self) -> None:
        manager = SimpleNamespace(
            output_state=SimpleNamespace(
                device_count=0,
                keyboard_uinput=None,
                mouse_uinput=None,
                gamepad_uinput=None,
            )
        )

        def fail_uinput(**_kwargs) -> FakeUInput:
            raise UInputError('"/dev/uinput" cannot be opened for writing')

        with pytest.raises(PermissionError) as excinfo:
            outputs.create_global_uinputs(
                manager,
                evdev_mod=SimpleNamespace(
                    ecodes=evdev.ecodes,
                    UInput=fail_uinput,
                    AbsInfo=evdev.AbsInfo,
                ),
                log=logging.getLogger("test"),
                uinput_writer=lambda device: device,
            )

        message = str(excinfo.value)
        assert "keyboard uinput device" in message
        assert UINPUT_PERMISSION_HINT in message
        assert manager.output_state.device_count == 0

    def test_configure_virtual_gamepads_logs_close_failures(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class _ClosingUInput:
            def __init__(self, exc: Exception) -> None:
                self.exc = exc

            def close(self) -> None:
                raise self.exc

        manager = SimpleNamespace(
            output_state=SimpleNamespace(
                virtual_gamepad_uinputs={
                    "virtual-gamepad-1": _ClosingUInput(OSError("device gone")),
                    "virtual-gamepad-2": _ClosingUInput(RuntimeError("close state invalid")),
                },
                virtual_gamepad_count=2,
            )
        )
        logger = logging.getLogger("keymasqd.devices")

        with caplog.at_level(logging.DEBUG, logger="keymasqd.devices"):
            count = outputs.configure_virtual_gamepads(
                manager,
                0,
                evdev_mod=SimpleNamespace(ecodes=evdev.ecodes),
                log=logger,
                uinput_writer=lambda device: device,
            )

        assert count == 0
        assert manager.output_state.virtual_gamepad_uinputs == {}
        assert "Failed to close virtual gamepad virtual-gamepad-1: device gone" in caplog.text
        assert "Unexpected failure closing virtual gamepad virtual-gamepad-2" in caplog.text
        assert "RuntimeError: close state invalid" in caplog.text

    def test_destroy_global_uinputs_logs_close_failures(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class _ClosingUInput:
            def __init__(self, exc: Exception) -> None:
                self.exc = exc

            def close(self) -> None:
                raise self.exc

        manager = SimpleNamespace(
            output_state=SimpleNamespace(
                device_count=1,
                keyboard_uinput=_ClosingUInput(OSError("keyboard gone")),
                mouse_uinput=_ClosingUInput(RuntimeError("mouse close state invalid")),
                virtual_gamepad_uinputs={},
            )
        )
        logger = logging.getLogger("keymasqd.devices")

        with caplog.at_level(logging.DEBUG, logger="keymasqd.devices"):
            outputs.destroy_global_uinputs(manager, log=logger)

        assert manager.output_state.device_count == 0
        assert manager.output_state.keyboard_uinput is None
        assert manager.output_state.mouse_uinput is None
        assert manager.output_state.virtual_gamepad_uinputs == {}
        assert "Failed to close global uinput device: keyboard gone" in caplog.text
        assert "Unexpected failure closing global uinput device" in caplog.text
        assert "RuntimeError: mouse close state invalid" in caplog.text

    @pytest.mark.asyncio
    async def test_grab_and_release_device_orchestrates_existing_and_removed_paths(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _RawInputDevice:
            def __init__(self, path: str) -> None:
                self.path = path

            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SIDE],
                    evdev.ecodes.EV_REL: [evdev.ecodes.REL_X, evdev.ecodes.REL_Y],
                }

        created: dict[str, object] = {}

        class _FakeManagedDevice:
            def __init__(self, **kwargs) -> None:
                self.path = kwargs["path"]
                self.interface_id = "mouse"
                self.button_map_updates: list[dict[str, str]] = []
                self.button_code_updates: list[dict[str, int]] = []
                self.grab = AsyncMock()
                self.release = AsyncMock()
                self.stop_event_loop = AsyncMock()
                self.reset_mapping_runtime_state = AsyncMock()
                created[self.path] = self

            def release_tracked_outputs(self) -> None:
                return

            def has_held_source_inputs(self) -> bool:
                return False

            def update_button_map(
                self,
                button_map: dict[str, str],
                button_codes: dict[str, int] | None = None,
                button_values: dict[str, int] | None = None,
            ) -> None:
                self.button_map_updates.append(dict(button_map))
                self.button_code_updates.append(dict(button_codes or {}))
                assert button_values is None or isinstance(button_values, dict)

        manager = DeviceManager()
        create_global_uinputs = Mock()
        destroy_global_uinputs = Mock()
        schedule_interface_release = Mock()
        cancel_pending_interface_release = Mock()

        monkeypatch.setattr(device_manager.evdev, "InputDevice", _RawInputDevice)
        monkeypatch.setattr(device_manager, "GrabbedDevice", _FakeManagedDevice)
        monkeypatch.setattr(device_manager, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(outputs, "create_global_uinputs", create_global_uinputs)
        monkeypatch.setattr(outputs, "destroy_global_uinputs", destroy_global_uinputs)
        monkeypatch.setattr(
            acquisition,
            "schedule_interface_release",
            schedule_interface_release,
        )
        monkeypatch.setattr(
            acquisition,
            "cancel_pending_interface_release",
            cancel_pending_interface_release,
        )

        first = await manager.grab_device(
            "1234:5678",
            ["/dev/input/event0", "/dev/input/event1"],
            {"left": "btn_side"},
        )
        second = await manager.grab_device(
            "1234:5678",
            ["/dev/input/event1"],
            {"right": "btn_side"},
        )
        released = await manager.release_device("1234:5678", immediate=True)

        assert first == {
            "grabbed": True,
            "hardware_id": "1234:5678",
            "grabbed_count": 2,
            "skipped_count": 0,
            "waiting_for_device": False,
        }
        assert second["grabbed_count"] == 2
        create_global_uinputs.assert_called_once()
        cancel_pending_interface_release.assert_called_once_with(
            manager, "1234:5678", "/dev/input/event1"
        )
        schedule_interface_release.assert_called_once_with(
            manager,
            "1234:5678",
            "/dev/input/event0",
            asyncio_mod=adapters.ASYNCIO_RUNTIME,
            log=release.log,
        )
        assert created["/dev/input/event1"].button_map_updates == [{"right": "btn_side"}]
        assert created["/dev/input/event1"].button_code_updates == [{}]
        assert released == {"released": True, "hardware_id": "1234:5678"}
        assert created["/dev/input/event0"].release.await_count == 1
        assert created["/dev/input/event1"].release.await_count == 1
        assert manager.grabbed_devices == {}
        assert manager.active_mappings == {}
        assert manager.grab_state.desired_paths == {}
        destroy_global_uinputs.assert_called_once()

    @pytest.mark.asyncio
    async def test_grab_skipped_probe_closes_raw_device(self) -> None:
        class _RawInputDevice:
            def __init__(self) -> None:
                self.close_count = 0

            def capabilities(self) -> dict[int, list[int]]:
                return {evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A]}

            def close(self) -> None:
                self.close_count += 1

        raw_device = _RawInputDevice()
        manager = DeviceManager()
        manager._device_input = lambda _path: raw_device  # type: ignore[method-assign]

        result = await manager.grab_device(
            "1234:5678",
            ["/dev/input/event0"],
            {},
        )

        assert result["grabbed_count"] == 0
        assert result["skipped_count"] == 1
        assert raw_device.close_count == 1

    @pytest.mark.asyncio
    async def test_grab_failure_closes_raw_probe_device(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _RawInputDevice:
            def __init__(self) -> None:
                self.close_count = 0

            def capabilities(self) -> dict[int, list[int]]:
                return {evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A]}

            def close(self) -> None:
                self.close_count += 1

        class _FailingManagedDevice:
            def __init__(self, **kwargs) -> None:
                self.path = kwargs["path"]

            async def grab(self) -> None:
                raise OSError(errno.EACCES, "denied")

        raw_device = _RawInputDevice()
        manager = DeviceManager()
        manager._device_input = lambda _path: raw_device  # type: ignore[method-assign]
        monkeypatch.setattr(manager, "_detect_device_types", lambda _device: ["keyboard"])
        monkeypatch.setattr(device_manager, "GrabbedDevice", _FailingManagedDevice)
        monkeypatch.setattr(outputs, "create_global_uinputs", Mock())
        monkeypatch.setattr(outputs, "destroy_global_uinputs", Mock())

        with pytest.raises(OSError):
            await manager.grab_device(
                "1234:5678",
                ["/dev/input/event0"],
                {"source": "key_a"},
            )

        assert raw_device.close_count == 1

    @pytest.mark.asyncio
    async def test_grab_with_retry_waits_for_busy_device_then_succeeds(self) -> None:
        sleep_calls: list[float] = []

        class _Asyncio:
            async def sleep(self, delay: float) -> None:
                sleep_calls.append(delay)

        class _BusyThenAvailableDevice:
            def __init__(self) -> None:
                self.attempts = 0

            async def grab(self) -> None:
                self.attempts += 1
                if self.attempts < 3:
                    raise OSError(errno.EBUSY, "busy")

        device = _BusyThenAvailableDevice()

        await acquisition.grab_with_retry(
            device,
            "/dev/input/event0",
            asyncio_mod=_Asyncio(),
            log=logging.getLogger("test"),
            errno_mod=errno,
        )

        assert device.attempts == 3
        assert sleep_calls == [0.05, 0.10]

    @pytest.mark.asyncio
    async def test_grab_with_retry_reraises_last_busy_error_after_retries(self) -> None:
        sleep_calls: list[float] = []

        class _Asyncio:
            async def sleep(self, delay: float) -> None:
                sleep_calls.append(delay)

        class _AlwaysBusyDevice:
            def __init__(self) -> None:
                self.attempts = 0

            async def grab(self) -> None:
                self.attempts += 1
                raise OSError(errno.EBUSY, "busy")

        device = _AlwaysBusyDevice()

        with pytest.raises(OSError, match="busy"):
            await acquisition.grab_with_retry(
                device,
                "/dev/input/event0",
                asyncio_mod=_Asyncio(),
                log=logging.getLogger("test"),
                errno_mod=errno,
            )

        assert device.attempts == 5
        assert sleep_calls == [0.05, 0.10, 0.20, 0.40]

    def test_pending_interface_release_cancellation_helpers_cancel_live_tasks(self) -> None:
        class _FakeTask:
            def __init__(self, *, done: bool = False) -> None:
                self._done = done
                self.cancelled = False

            def done(self) -> bool:
                return self._done

            def cancel(self) -> None:
                self.cancelled = True

        manager = DeviceManager()
        path_task = _FakeTask()
        done_task = _FakeTask(done=True)
        hardware_task = _FakeTask()
        other_hardware_task = _FakeTask()

        manager.grab_state.pending_interface_release[("hw", "/dev/input/event0")] = path_task
        manager.grab_state.pending_interface_release[("hw", "/dev/input/event1")] = done_task
        manager.grab_state.pending_interface_release[("hw", "/dev/input/event2")] = hardware_task
        manager.grab_state.pending_interface_release[("other", "/dev/input/event3")] = (
            other_hardware_task
        )

        release.cancel_pending_interface_release(manager, "hw", "/dev/input/event0")
        release.cancel_pending_interface_releases_for_hardware(manager, "hw")

        assert path_task.cancelled is True
        assert done_task.cancelled is False
        assert hardware_task.cancelled is True
        assert other_hardware_task.cancelled is False
        assert manager.grab_state.pending_interface_release == {
            ("other", "/dev/input/event3"): other_hardware_task,
        }

    @pytest.mark.asyncio
    async def test_delayed_interface_release_keeps_currently_desired_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        release_interface = AsyncMock()
        key = ("hw", "/dev/input/event0")
        manager.grab_state.desired_paths["hw"] = {key[1]}
        monkeypatch.setattr(release, "release_interface_unlocked", release_interface)

        task = asyncio.create_task(
            release.delayed_interface_release(
                manager,
                key[0],
                key[1],
                0.001,
                asyncio_mod=adapters.ASYNCIO_RUNTIME,
            )
        )
        manager.grab_state.pending_interface_release[key] = task

        await task

        release_interface.assert_not_awaited()
        assert key not in manager.grab_state.pending_interface_release

    @pytest.mark.asyncio
    async def test_delayed_interface_release_cleans_up_after_cancellation(self) -> None:
        manager = DeviceManager()
        key = ("hw", "/dev/input/event0")
        task = asyncio.create_task(
            release.delayed_interface_release(
                manager,
                key[0],
                key[1],
                60.0,
                asyncio_mod=adapters.ASYNCIO_RUNTIME,
            )
        )
        manager.grab_state.pending_interface_release[key] = task

        await asyncio.sleep(0)
        task.cancel()
        await task

        assert key not in manager.grab_state.pending_interface_release

    @pytest.mark.asyncio
    async def test_release_interface_keeps_remaining_managed_devices(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        removed = SimpleNamespace(
            path="/dev/input/event0",
            interface_id="kbd",
            stop_event_loop=AsyncMock(),
            release_tracked_outputs=Mock(),
            release=AsyncMock(),
        )
        kept = SimpleNamespace(
            path="/dev/input/event1",
            interface_id="mouse",
            release_tracked_outputs=Mock(),
            release=AsyncMock(),
        )
        manager = DeviceManager()
        manager.grabbed_devices = {"hw": [removed, kept]}
        clear_combo_scope = AsyncMock()
        monkeypatch.setattr(lifecycle, "clear_combo_runtime_for_binding_scope", clear_combo_scope)

        await release.release_interface_unlocked(manager, "hw", "/dev/input/event0")

        assert manager.grabbed_devices == {"hw": [kept]}
        clear_combo_scope.assert_awaited_once()
        removed.release_tracked_outputs.assert_called_once()
        removed.release.assert_awaited_once()
        kept.release.assert_not_awaited()

    def test_grab_lifecycle_helpers_ignore_malformed_capabilities_and_analog_inputs(
        self,
    ) -> None:
        caps = {
            evdev.ecodes.EV_SYN: [evdev.ecodes.SYN_REPORT],
            evdev.ecodes.EV_KEY: [
                (),
                ("not-an-int",),
                "not-a-code",
                (evdev.ecodes.KEY_A,),
                evdev.ecodes.KEY_B,
            ],
        }
        analog_inputs = {
            "not-a-table": "bad",
            "axes-not-list": {"axes": "bad"},
            "axis-not-table": {"axes": ["bad"]},
            "bad-code": {"axes": [{"evdev_code": "not-an-int"}]},
            "hex-code": {"axes": [{"evdev_code": "0x1"}]},
            "named-code": {"axes": [{"evdev": "abs_x"}]},
        }

        assert planning.device_has_mapped_buttons(
            caps, {"key_b"}, None, evdev_mod=device_manager.evdev
        )
        assert not planning.device_has_mapped_buttons(
            {999999: [1]}, set(), None, evdev_mod=device_manager.evdev
        )
        assert planning.analog_input_bindings(analog_inputs) == {
            (evdev.ecodes.EV_ABS, 1),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X),
        }

    def test_parse_action_supports_string_and_compositor_dispatch(self) -> None:
        manager = DeviceManager()
        string_action = action_parser.parse_action(
            manager,
            "key_a",
        )
        dispatch_action = action_parser.parse_action(
            manager,
            {
                "action": "compositor_dispatch",
                "compositor": "hyprland",
                "dispatcher": "workspace",
                "args": "2",
            },
        )
        repeat_action = action_parser.parse_action(
            manager,
            {
                "action": "repeat",
                "repeat_categories": ["keyboard", "mouse_button", "mouse_wheel"],
                "rapidfire_enabled": True,
                "rapidfire_hold_ms": 30,
                "rapidfire_wait_ms": 40,
            },
        )

        assert string_action.action_type == ActionType.KEYBOARD
        assert string_action.target == "key_a"
        assert dispatch_action.action_type == ActionType.COMPOSITOR_DISPATCH
        assert dispatch_action.compositor_id == "hyprland"
        assert dispatch_action.compositor_dispatcher == "workspace"
        assert dispatch_action.compositor_args == "2"
        assert repeat_action.action_type == ActionType.REPEAT
        assert repeat_action.repeat_categories == ["keyboard", "mouse"]
        assert repeat_action.rapidfire_enabled is True
        assert repeat_action.rapidfire_hold_ms == 30
        assert repeat_action.rapidfire_wait_ms == 40

    @pytest.mark.asyncio
    async def test_mapping_and_combo_updates_prune_stale_exec_repeat_history(self) -> None:
        from keymasq.keymasqd.runtime.repeat import RepeatHistoryEntry

        manager = DeviceManager()
        manager.grabbed_devices["kbd"] = []
        manager.repeat_state.history.extend(
            [
                RepeatHistoryEntry(
                    category="special",
                    action=MappingAction(action_type=ActionType.EXEC, exec_ref=1),
                    source_device="kbd",
                    source_button="key_f13",
                ),
                RepeatHistoryEntry(
                    category="special",
                    action=MappingAction(action_type=ActionType.EXEC, exec_ref=2),
                    source_device="mouse",
                    source_button="btn_side",
                ),
                RepeatHistoryEntry(
                    category="special",
                    action=MappingAction(action_type=ActionType.EXEC, exec_ref=3),
                    source_device="mouse",
                    source_button="combo:launch",
                ),
                RepeatHistoryEntry(
                    category="special",
                    action=MappingAction(action_type=ActionType.EXEC, exec_ref=4),
                    source_device="kbd",
                    source_button="combo:kbd-launch",
                ),
                RepeatHistoryEntry(
                    category="keyboard",
                    action=MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
                    source_device="kbd",
                    source_button="key_a",
                ),
            ]
        )

        await manager.set_mapping("kbd", {})

        assert [entry.action.exec_ref for entry in manager.repeat_state.history] == [
            2,
            3,
            4,
            None,
        ]

        await manager.set_combos([])

        assert [entry.action.exec_ref for entry in manager.repeat_state.history] == [
            2,
            None,
        ]

    def test_forget_exec_actions_without_filters_prunes_global_exec_history(self) -> None:
        from keymasq.keymasqd.runtime.repeat import RepeatHistoryEntry, forget_exec_actions

        manager = DeviceManager()
        manager.repeat_state.history.extend(
            [
                RepeatHistoryEntry(
                    category="special",
                    action=MappingAction(action_type=ActionType.EXEC, exec_ref=1),
                    source_device="kbd",
                    source_button="key_f13",
                ),
                RepeatHistoryEntry(
                    category="keyboard",
                    action=MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
                    source_device="kbd",
                    source_button="key_a",
                ),
            ]
        )

        forget_exec_actions(manager.repeat_state)

        assert [entry.action.action_type for entry in manager.repeat_state.history] == [
            ActionType.KEYBOARD,
        ]

    def test_forget_exec_actions_prunes_superkey_history_with_nested_exec_refs(
        self,
    ) -> None:
        from keymasq.keymasqd.runtime.repeat import (
            SUPERKEY_SLOT_OVERLOAD,
            SUPERKEY_SLOT_TAP,
            RepeatHistoryEntry,
            forget_exec_actions,
        )

        manager = DeviceManager()
        manager.repeat_state.history.extend(
            [
                RepeatHistoryEntry(
                    category="special",
                    action=MappingAction(
                        action_type=ActionType.SUPERKEY,
                        superkey_config=SuperkeyConfig(
                            name="pattern-exec",
                            tap_actions=[
                                SuperkeyActionData(action_type="exec", exec_ref=10),
                            ],
                        ),
                    ),
                    source_device="kbd",
                    source_button="key_f13",
                    superkey_slot=SUPERKEY_SLOT_TAP,
                ),
                RepeatHistoryEntry(
                    category="special",
                    action=MappingAction(
                        action_type=ActionType.SUPERKEY,
                        superkey_config=SuperkeyConfig(
                            name="other-device-pattern-exec",
                            tap_actions=[
                                SuperkeyActionData(action_type="exec", exec_ref=11),
                            ],
                        ),
                    ),
                    source_device="mouse",
                    source_button="btn_side",
                    superkey_slot=SUPERKEY_SLOT_TAP,
                ),
                RepeatHistoryEntry(
                    category="special",
                    action=MappingAction(
                        action_type=ActionType.SUPERKEY,
                        superkey_config=SuperkeyConfig(
                            name="combo-overload-exec",
                            mode=SuperkeyMode.OVERLOAD,
                            overload_actions=[
                                MappingAction(action_type=ActionType.EXEC, exec_ref=12),
                            ],
                        ),
                    ),
                    source_device="kbd",
                    source_button="combo:launch",
                    superkey_slot=SUPERKEY_SLOT_OVERLOAD,
                ),
                RepeatHistoryEntry(
                    category="special",
                    action=MappingAction(
                        action_type=ActionType.SUPERKEY,
                        superkey_config=SuperkeyConfig(
                            name="pattern-key",
                            tap_actions=[
                                SuperkeyActionData(
                                    action_type="keyboard",
                                    target="key_a",
                                ),
                            ],
                        ),
                    ),
                    source_device="kbd",
                    source_button="key_f14",
                    superkey_slot=SUPERKEY_SLOT_TAP,
                ),
                RepeatHistoryEntry(
                    category="special",
                    action=MappingAction(action_type=ActionType.EXEC, exec_ref=13),
                    source_device="kbd",
                    source_button="key_f15",
                ),
            ]
        )

        forget_exec_actions(
            manager.repeat_state,
            source_device="kbd",
            exclude_source_button_prefix="combo:",
        )

        assert [entry.source_button for entry in manager.repeat_state.history] == [
            "btn_side",
            "combo:launch",
            "key_f14",
        ]

    def test_forget_exec_actions_checks_remembered_superkey_slot(self) -> None:
        from keymasq.keymasqd.runtime.repeat import (
            SUPERKEY_SLOT_HOLD,
            SUPERKEY_SLOT_TAP,
            RepeatHistoryEntry,
            forget_exec_actions,
        )

        manager = DeviceManager()
        superkey_config = SuperkeyConfig(
            name="mixed",
            tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
            hold_actions=[SuperkeyActionData(action_type="exec", exec_ref=14)],
        )
        manager.repeat_state.history.extend(
            [
                RepeatHistoryEntry(
                    category="special",
                    action=MappingAction(
                        action_type=ActionType.SUPERKEY,
                        superkey_config=superkey_config,
                    ),
                    source_device="kbd",
                    source_button="key_f16",
                    superkey_slot=SUPERKEY_SLOT_TAP,
                ),
                RepeatHistoryEntry(
                    category="special",
                    action=MappingAction(
                        action_type=ActionType.SUPERKEY,
                        superkey_config=superkey_config,
                    ),
                    source_device="kbd",
                    source_button="key_f17",
                    superkey_slot=SUPERKEY_SLOT_HOLD,
                ),
            ]
        )

        forget_exec_actions(manager.repeat_state, source_device="kbd")

        assert [entry.superkey_slot for entry in manager.repeat_state.history] == [
            SUPERKEY_SLOT_TAP,
        ]

    def test_parse_action_warns_and_strips_unsupported_rapidfire(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        manager = DeviceManager()

        with caplog.at_level("WARNING", logger="keymasqd.runtime.action_parser"):
            action = action_parser.parse_action(
                manager,
                {
                    "action": "exec",
                    "cmd": "echo hi",
                    "rapidfire_enabled": True,
                    "rapidfire_hold_ms": 40,
                    "rapidfire_wait_ms": 60,
                },
            )

        assert action.action_type == ActionType.EXEC
        assert action.rapidfire_enabled is False
        assert action.rapidfire_hold_ms == 20
        assert action.rapidfire_wait_ms == 20
        assert "Ignoring rapidfire for unsupported exec action in runtime payload" in caplog.text

    @pytest.mark.asyncio
    async def test_set_combos_skips_malformed_entries_and_parses_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        clear_combo_runtime_unlocked = AsyncMock()
        refresh_combo_timeout_watchdog = Mock()
        monkeypatch.setattr(
            lifecycle,
            "clear_combo_runtime_unlocked",
            clear_combo_runtime_unlocked,
        )
        monkeypatch.setattr(
            lifecycle,
            "refresh_combo_timeout_watchdog",
            refresh_combo_timeout_watchdog,
        )

        result = await manager.set_combos(
            [
                "bad",
                {"id": "missing-action", "steps": []},
                {
                    "id": "valid",
                    "name": "Valid",
                    "steps": [
                        "bad-step",
                        {
                            "events": [
                                "bad-event",
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_a",
                                },
                            ],
                            "timeout_ms": "250",
                        },
                    ],
                    "action": {"action": "suppress"},
                },
            ]
        )

        assert result == {"updated": True, "combo_count": 1}
        assert len(manager.combo_state.active_combos) == 1
        assert manager.combo_state.active_combos[0].steps[0].timeout_ms == 250
        clear_combo_runtime_unlocked.assert_awaited_once()
        refresh_combo_timeout_watchdog.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_combos_parses_superkey_combo_action(self) -> None:
        manager = DeviceManager()

        result = await manager.set_combos(
            [
                {
                    "id": "superkey-combo",
                    "name": "Superkey Combo",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_a",
                                }
                            ]
                        }
                    ],
                    "action": {
                        "action": "superkey",
                        "superkey": {
                            "name": "combo-pattern",
                            "mode": "pattern",
                            "tap_actions": [{"action": "keyboard", "target": "key_b"}],
                            "double_tap_actions": [{"action": "keyboard", "target": "key_c"}],
                        },
                    },
                }
            ]
        )

        assert result == {"updated": True, "combo_count": 1}
        assert len(manager.combo_state.active_combos) == 1
        action = manager.combo_state.active_combos[0].action
        assert action is not None
        assert action.action_type == ActionType.SUPERKEY
        assert action.superkey_config is not None
        assert action.superkey_config.mode == SuperkeyMode.PATTERN
        assert action.superkey_config.tap_actions[0].target == "key_b"

    @pytest.mark.asyncio
    async def test_set_combos_parses_trigger_recall_settings(self) -> None:
        manager = DeviceManager()

        result = await manager.set_combos(
            [
                {
                    "id": "recall-combo",
                    "name": "Recall Combo",
                    "recall_trigger_keys": True,
                    "restore_trigger_keys": ["meta", "key_c", "meta"],
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "meta",
                                },
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_c",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "suppress"},
                }
            ]
        )

        assert result == {"updated": True, "combo_count": 1}
        assert len(manager.combo_state.active_combos) == 1
        assert manager.combo_state.active_combos[0].recall_trigger_keys is True
        assert manager.combo_state.active_combos[0].restore_trigger_keys == ["meta", "key_c"]

    @pytest.mark.asyncio
    async def test_schedule_topology_reconcile_logs_failures_and_clears_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        manager = DeviceManager(topology_debounce_s=0.01)
        snapshot: dict[str, topology.LiveInterfaceInfo] = {}

        async def fake_sleep(_delay: float) -> None:
            return

        reconcile_topology = AsyncMock(side_effect=RuntimeError("reconcile boom"))
        monkeypatch.setattr(device_manager.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(topology, "reconcile_topology", reconcile_topology)

        with caplog.at_level(logging.WARNING, logger="keymasqd.devices"):
            topology.schedule_topology_reconcile(
                manager,
                snapshot,
                log=device_manager.log,
                deps=device_manager._topology_runtime_deps(),
            )
            task = manager.topology_state.reconcile_task
            assert task is not None
            await task

        assert "Topology reconcile failed: reconcile boom" in caplog.text
        assert manager.topology_state.reconcile_task is None

    def test_combo_capture_queue_round_trip(self) -> None:
        manager = DeviceManager()
        ready = asyncio.Event()
        manager.grabbed_devices = {"hw": [object(), object()], "other": [object()]}

        started = manager.begin_combo_capture("token", {"1234:5678"}, ready)
        capture_queue, hardware_ids, notify_event = manager.combo_state.capture_queues["token"]
        capture_queue.put({"evdev": "key_a"})

        assert started == {"token": "token", "grabbed_devices": 3}
        assert hardware_ids == {"1234:5678"}
        assert notify_event is ready
        assert manager.read_combo_capture("token") == {"event": {"evdev": "key_a"}}
        assert manager.read_combo_capture("token") == {"event": None}
        assert manager.end_combo_capture("token") == {"status": "ok", "ended": True}
        assert manager.end_combo_capture("token") == {"status": "ok", "ended": False}

    def test_combo_capture_queue_ignores_unmatched_hardware_filter(self) -> None:
        manager = DeviceManager()
        ready = asyncio.Event()
        manager.begin_combo_capture("token", {"device-a"}, ready)

        consumed = events.queue_combo_capture_event(
            manager,
            {"hardware_id": "device-b", "evdev": "key_a", "value": 1},
            str_value_fn=lambda value, default: default if value is None else str(value),
        )

        assert consumed is False
        assert ready.is_set() is False
        assert manager.read_combo_capture("token") == {"event": None}

    @pytest.mark.asyncio
    async def test_refresh_combo_timeout_watchdog_cancels_or_replaces_existing_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        previous = asyncio.create_task(asyncio.sleep(60))
        manager.combo_state.timeout_task = previous
        manager.combo_state.progression.engine.next_deadline = Mock(return_value=None)  # type: ignore[method-assign]

        lifecycle.refresh_combo_timeout_watchdog(manager, deps=combo_runtime_deps())
        await asyncio.sleep(0)

        assert previous.cancelled() is True
        assert manager.combo_state.timeout_task is None

        replacement = asyncio.create_task(asyncio.sleep(60))
        manager.combo_state.timeout_task = replacement
        manager.combo_state.progression.engine.next_deadline = Mock(return_value=42.0)  # type: ignore[method-assign]
        combo_timeout_watchdog = AsyncMock()
        monkeypatch.setattr(
            lifecycle,
            "combo_timeout_watchdog",
            combo_timeout_watchdog,
        )

        lifecycle.refresh_combo_timeout_watchdog(manager, deps=combo_runtime_deps())
        await asyncio.sleep(0)

        assert replacement.cancelled() is True
        combo_timeout_watchdog.assert_awaited_once()
        manager.combo_state.timeout_task.cancel()
        await asyncio.sleep(0)
        assert manager.combo_state.timeout_task.done() is True

    @pytest.mark.asyncio
    async def test_combo_timeout_watchdog_expires_and_clears_current_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        manager.combo_state.progression.engine.expire_timeouts = Mock()  # type: ignore[method-assign]
        refreshes: list[str] = []
        monkeypatch.setattr(
            lifecycle,
            "refresh_combo_timeout_watchdog",
            Mock(side_effect=lambda *args, **kwargs: refreshes.append("refresh")),
        )

        monkeypatch.setattr(lifecycle.time, "monotonic", lambda: 10.0)
        monkeypatch.setattr(device_manager.asyncio, "sleep", AsyncMock())

        task = asyncio.create_task(
            lifecycle.combo_timeout_watchdog(manager, 10.5, deps=combo_runtime_deps())
        )
        manager.combo_state.timeout_task = task
        await task

        manager.combo_state.progression.engine.expire_timeouts.assert_called_once_with(10.0)
        assert manager.combo_state.timeout_task is None
        assert refreshes == ["refresh"]
