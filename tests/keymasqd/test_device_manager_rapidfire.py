import asyncio
import contextlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import evdev
import pytest

from keymasq.common.models import ActionType, DeviceType, MappingAction
from keymasq.keymasqd import device_manager as dm
from keymasq.keymasqd.combo_engine import ComboDecision
from keymasq.keymasqd.device_manager import DesiredGrabConfig, DeviceManager
from keymasq.keymasqd.runtime import combos as cdm
from keymasq.keymasqd.runtime import grab_lifecycle as ldm
from keymasq.keymasqd.runtime import grabbed_device as gdm
from keymasq.keymasqd.runtime import grabbed_device_actions as gda
from keymasq.keymasqd.runtime import grabbed_device_events as gde
from keymasq.keymasqd.runtime import grabbed_device_grab as gdg
from keymasq.keymasqd.runtime import grabbed_device_repeat as gdr
from keymasq.keymasqd.runtime.grabbed_device import GrabbedDevice
from tests.keymasqd.device_manager_support import (
    FakeUInput,
    combo_runtime_deps,
    grabbed_event_processing_deps,
)


class TestRapidfireRelease:
    @pytest.mark.asyncio
    async def test_set_virtual_gamepads_waits_for_cancelled_rapidfire_finalizers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.device_count = 1
        manager.output_state.virtual_gamepad_count = 1
        finalized: list[str] = []

        async def rapidfire_task() -> None:
            try:
                await asyncio.Future()
            finally:
                finalized.append("rapidfire")

        task = asyncio.create_task(rapidfire_task())
        await asyncio.sleep(0)
        state = SimpleNamespace(
            rapidfire_tasks={"btn": task},
            rapidfire_outputs={},
            rapidfire_active={"btn": True},
            tap_active={},
        )
        device = SimpleNamespace(
            state=state,
            release_tracked_outputs=lambda: None,
            reset_mapping_runtime_state=AsyncMock(),
            reset_superkeys=AsyncMock(),
        )
        manager.grabbed_devices = {"device": [device]}

        async def fake_clear_combo_runtime(*_args, **_kwargs) -> None:
            return None

        def fake_configure_virtual_gamepads(*_args, **_kwargs) -> int:
            assert finalized == ["rapidfire"]
            manager.output_state.virtual_gamepad_count = 2
            return 2

        monkeypatch.setattr(dm.runtime_combos, "clear_combo_runtime", fake_clear_combo_runtime)
        monkeypatch.setattr(
            dm.runtime_outputs,
            "configure_virtual_gamepads",
            fake_configure_virtual_gamepads,
        )

        result = await manager.set_virtual_gamepads(2)

        assert result == {"status": "ok", "count": 2}
        assert task.done()

    @pytest.mark.asyncio
    async def test_set_virtual_gamepads_reuses_device_runtime_reset(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.device_count = 1
        manager.output_state.virtual_gamepad_count = 1
        device = SimpleNamespace(
            state=SimpleNamespace(rapidfire_tasks={}),
            release_tracked_outputs=Mock(),
            reset_mapping_runtime_state=AsyncMock(),
            reset_superkeys=AsyncMock(),
        )
        manager.grabbed_devices = {"device": [device]}

        async def fake_clear_combo_runtime(*_args, **_kwargs) -> None:
            return None

        def fake_configure_virtual_gamepads(*_args, **_kwargs) -> int:
            manager.output_state.virtual_gamepad_count = 2
            return 2

        monkeypatch.setattr(dm.runtime_combos, "clear_combo_runtime", fake_clear_combo_runtime)
        monkeypatch.setattr(
            dm.runtime_outputs,
            "configure_virtual_gamepads",
            fake_configure_virtual_gamepads,
        )

        result = await manager.set_virtual_gamepads(2)

        assert result == {"status": "ok", "count": 2}
        device.release_tracked_outputs.assert_called_once()
        device.reset_mapping_runtime_state.assert_awaited_once()
        device.reset_superkeys.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_grab_waits_until_active_keys_clear_before_grabbing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")
        call_order: list[str] = []

        class _FakeInputDevice:
            def __init__(self) -> None:
                self._active_keys = [
                    [evdev.ecodes.KEY_L],
                    [],
                ]
                self.grab_calls = 0

            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_L],
                    evdev.ecodes.EV_SYN: [],
                }

            def active_keys(self) -> list[int]:
                call_order.append("active_keys")
                if len(self._active_keys) > 1:
                    return self._active_keys.pop(0)
                return self._active_keys[0]

            def grab(self) -> None:
                call_order.append("grab")
                self.grab_calls += 1

        fake_input = _FakeInputDevice()
        created_tasks: list[asyncio.Task] = []
        to_thread_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        wait_timeouts: list[float] = []
        original_create_task = asyncio.create_task
        original_sleep = asyncio.sleep

        async def fake_to_thread(func, /, *args, **kwargs):
            to_thread_calls.append((func, args, kwargs))
            return func(*args, **kwargs)

        async def fake_wait_for_active_key_activity(timeout_s: float) -> bool:
            wait_timeouts.append(timeout_s)
            return True

        def fake_create_task(coro):
            coro.close()
            task = original_create_task(original_sleep(0))
            created_tasks.append(task)
            return task

        monkeypatch.setattr(gdm.evdev, "InputDevice", lambda _path: fake_input)
        monkeypatch.setattr(
            gdm.evdev,
            "UInput",
            lambda *args, **kwargs: call_order.append("uinput") or FakeUInput(*args, **kwargs),
        )
        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(gdm.asyncio, "create_task", fake_create_task)
        monkeypatch.setattr(
            gdg,
            "wait_for_active_key_activity",
            lambda _device, timeout_s, **_kwargs: fake_wait_for_active_key_activity(timeout_s),
        )

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=FakeUInput(),  # type: ignore[arg-type]
        )

        await device.grab()
        await original_sleep(0)

        assert wait_timeouts == [pytest.approx(gdm.ACTIVE_KEY_IDLE_LOG_INTERVAL_S)]
        assert [call[0] for call in to_thread_calls] == [
            fake_input.active_keys,
            fake_input.active_keys,
        ]
        assert fake_input.grab_calls == 1
        assert isinstance(device.uinput, FakeUInput)
        assert call_order == ["uinput", "active_keys", "active_keys", "grab"]
        assert created_tasks

    @pytest.mark.asyncio
    async def test_wait_for_active_keys_logs_progress_while_delaying_grab(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        class _FakeInputDevice:
            def __init__(self) -> None:
                self._active_keys = [
                    [evdev.ecodes.KEY_L],
                    [evdev.ecodes.KEY_L],
                    [evdev.ecodes.KEY_L],
                    [],
                ]

            def active_keys(self) -> list[int]:
                if len(self._active_keys) > 1:
                    return self._active_keys.pop(0)
                return self._active_keys[0]

        fake_input = _FakeInputDevice()
        to_thread_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        wait_timeouts: list[float] = []
        monotonic_values = iter([0.0, 0.0, 0.2, 1.3])
        monotonic_last = {"value": 1.3}

        async def fake_to_thread(func, /, *args, **kwargs):
            to_thread_calls.append((func, args, kwargs))
            return func(*args, **kwargs)

        async def fake_wait_for_active_key_activity(timeout_s: float) -> bool:
            wait_timeouts.append(timeout_s)
            return len(wait_timeouts) != 2

        def fake_monotonic() -> float:
            try:
                monotonic_last["value"] = next(monotonic_values)
            except StopIteration:
                pass
            return monotonic_last["value"]

        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(gdm.time, "monotonic", fake_monotonic)
        monkeypatch.setattr(
            gdg,
            "wait_for_active_key_activity",
            lambda _device, timeout_s, **_kwargs: fake_wait_for_active_key_activity(timeout_s),
        )

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=FakeUInput(),  # type: ignore[arg-type]
        )
        device.device = fake_input  # type: ignore[assignment]

        with caplog.at_level(logging.INFO, logger="keymasqd.devices"):
            await gdg.wait_for_active_keys_to_clear(
                device,
                asyncio_mod=gdm.ASYNCIO_RUNTIME,
                time_mod=gdm.time,
                log=gdm.log,
                active_key_idle_max_wait_s=gdm.ACTIVE_KEY_IDLE_MAX_WAIT_S,
                active_key_idle_log_interval_s=gdm.ACTIVE_KEY_IDLE_LOG_INTERVAL_S,
            )

        assert wait_timeouts == pytest.approx([1.0, 0.8, 1.0])
        assert len(to_thread_calls) == 4
        assert all(call[0] == fake_input.active_keys for call in to_thread_calls)
        assert "delaying grab until keys are released: key_l" in caplog.text
        assert "still waiting to grab; active keys still down: key_l" in caplog.text
        assert "active keys cleared, proceeding with grab" in caplog.text

    @pytest.mark.asyncio
    async def test_wait_for_active_keys_logs_read_failure_and_proceeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        class _FakeInputDevice:
            def active_keys(self) -> list[int]:
                raise OSError("broken active_keys")

        to_thread_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

        async def fake_to_thread(func, /, *args, **kwargs):
            to_thread_calls.append((func, args, kwargs))
            return func(*args, **kwargs)

        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=FakeUInput(),  # type: ignore[arg-type]
        )
        device.device = _FakeInputDevice()  # type: ignore[assignment]

        with caplog.at_level(logging.WARNING, logger="keymasqd.devices"):
            await gdg.wait_for_active_keys_to_clear(
                device,
                asyncio_mod=gdm.ASYNCIO_RUNTIME,
                time_mod=gdm.time,
                log=gdm.log,
                active_key_idle_max_wait_s=gdm.ACTIVE_KEY_IDLE_MAX_WAIT_S,
                active_key_idle_log_interval_s=gdm.ACTIVE_KEY_IDLE_LOG_INTERVAL_S,
            )

        assert [call[0] for call in to_thread_calls] == [device.device.active_keys]
        assert "failed to read active keys before grab: broken active_keys" in caplog.text
        assert "proceeding with grab" in caplog.text

    @pytest.mark.asyncio
    async def test_wait_for_active_keys_logs_unexpected_read_failure_and_proceeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        class _FakeInputDevice:
            def active_keys(self) -> list[int]:
                raise RuntimeError("broken active_keys")

        to_thread_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

        async def fake_to_thread(func, /, *args, **kwargs):
            to_thread_calls.append((func, args, kwargs))
            return func(*args, **kwargs)

        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=FakeUInput(),  # type: ignore[arg-type]
        )
        device.device = _FakeInputDevice()  # type: ignore[assignment]

        with caplog.at_level(logging.ERROR, logger="keymasqd.devices"):
            await gdg.wait_for_active_keys_to_clear(
                device,
                asyncio_mod=gdm.ASYNCIO_RUNTIME,
                time_mod=gdm.time,
                log=gdm.log,
                active_key_idle_max_wait_s=gdm.ACTIVE_KEY_IDLE_MAX_WAIT_S,
                active_key_idle_log_interval_s=gdm.ACTIVE_KEY_IDLE_LOG_INTERVAL_S,
            )

        assert [call[0] for call in to_thread_calls] == [device.device.active_keys]
        assert "unexpected failure reading active keys before grab" in caplog.text
        assert "proceeding with grab" in caplog.text
        assert "RuntimeError: broken active_keys" in caplog.text

    @pytest.mark.asyncio
    async def test_wait_for_active_keys_times_out_with_clear_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")
        monkeypatch.setattr(gdm, "ACTIVE_KEY_IDLE_MAX_WAIT_S", 60.0)

        class _FakeInputDevice:
            def active_keys(self) -> list[int]:
                return [evdev.ecodes.KEY_L]

        wait_timeouts: list[float] = []
        monotonic_values = iter([0.0, 61.0])
        monotonic_last = {"value": 61.0}

        async def fake_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        async def fake_wait_for_active_key_activity(timeout_s: float) -> bool:
            wait_timeouts.append(timeout_s)
            return False

        def fake_monotonic() -> float:
            try:
                monotonic_last["value"] = next(monotonic_values)
            except StopIteration:
                pass
            return monotonic_last["value"]

        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(gdm.time, "monotonic", fake_monotonic)
        monkeypatch.setattr(
            gdg,
            "wait_for_active_key_activity",
            lambda _device, timeout_s, **_kwargs: fake_wait_for_active_key_activity(timeout_s),
        )

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=FakeUInput(),  # type: ignore[arg-type]
        )
        device.device = _FakeInputDevice()  # type: ignore[assignment]

        with caplog.at_level(logging.ERROR, logger="keymasqd.devices"):
            with pytest.raises(TimeoutError, match="timed out waiting 60.0s"):
                await gdg.wait_for_active_keys_to_clear(
                    device,
                    asyncio_mod=gdm.ASYNCIO_RUNTIME,
                    time_mod=gdm.time,
                    log=gdm.log,
                    active_key_idle_max_wait_s=gdm.ACTIVE_KEY_IDLE_MAX_WAIT_S,
                    active_key_idle_log_interval_s=gdm.ACTIVE_KEY_IDLE_LOG_INTERVAL_S,
                )

        assert wait_timeouts == []
        assert "timed out waiting 60.0s for active keys to clear before grab" in caplog.text

    @pytest.mark.asyncio
    async def test_grab_closes_precreated_uinput_when_wait_times_out(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")
        monkeypatch.setattr(gdm, "ACTIVE_KEY_IDLE_MAX_WAIT_S", 60.0)

        class _FakeInputDevice:
            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_L],
                    evdev.ecodes.EV_SYN: [],
                }

            def active_keys(self) -> list[int]:
                return [evdev.ecodes.KEY_L]

        class _ClosableUInput(FakeUInput):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.close_calls = 0

            def close(self) -> None:
                self.close_calls += 1

        monotonic_values = iter([0.0, 61.0])
        monotonic_last = {"value": 61.0}
        created_uinputs: list[_ClosableUInput] = []

        async def fake_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        def fake_monotonic() -> float:
            try:
                monotonic_last["value"] = next(monotonic_values)
            except StopIteration:
                pass
            return monotonic_last["value"]

        def fake_uinput(*args, **kwargs) -> _ClosableUInput:
            uinput = _ClosableUInput(*args, **kwargs)
            created_uinputs.append(uinput)
            return uinput

        monkeypatch.setattr(gdm.evdev, "InputDevice", lambda _path: _FakeInputDevice())
        monkeypatch.setattr(gdm.evdev, "UInput", fake_uinput)
        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(gdm.time, "monotonic", fake_monotonic)

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=FakeUInput(),  # type: ignore[arg-type]
        )

        with pytest.raises(TimeoutError, match="timed out waiting 60.0s"):
            await device.grab()

        assert len(created_uinputs) == 1
        assert created_uinputs[0].close_calls == 1
        assert device.uinput is None

    @pytest.mark.asyncio
    async def test_grab_uses_explicit_passthrough_test_uinput_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KEYMASQ_TEST_UINPUT", "1")
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        class _FakeInputDevice:
            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_L],
                    evdev.ecodes.EV_SYN: [],
                }

            def active_keys(self) -> list[int]:
                return []

            def grab(self) -> None:
                return

        created_tasks: list[asyncio.Task[None]] = []
        original_create_task = asyncio.create_task

        async def fake_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        def fake_create_task(coro):
            coro.close()
            task = original_create_task(asyncio.sleep(0))
            created_tasks.append(task)
            return task

        monkeypatch.setattr(gdm.evdev, "InputDevice", lambda _path: _FakeInputDevice())
        monkeypatch.setattr(gdm.evdev, "UInput", FakeUInput)
        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(gdm.asyncio, "create_task", fake_create_task)

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=FakeUInput(),  # type: ignore[arg-type]
        )

        await device.grab()
        await asyncio.sleep(0)

        assert isinstance(device.uinput, FakeUInput)
        assert device.uinput.kwargs["name"] == "keymasq-test-passthrough-1234:5678"
        assert device.uinput.kwargs["vendor"] == 0x4B46
        assert device.uinput.kwargs["product"] == 0x1004

        for task in created_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_gamepad_passthrough_copies_source_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("KEYMASQ_TEST_UINPUT", raising=False)
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "js")

        class _FakeInputDevice:
            name = "Xbox 360 Wireless Controller"
            info = SimpleNamespace(
                vendor=0x045E,
                product=0x02A1,
                version=0x0114,
                bustype=0x0003,
            )

            def capabilities(self) -> dict[int, list[object]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
                    evdev.ecodes.EV_ABS: [
                        (
                            evdev.ecodes.ABS_X,
                            evdev.AbsInfo(0, -32768, 32767, 16, 128, 0),
                        )
                    ],
                    evdev.ecodes.EV_SYN: [],
                }

            def input_props(self) -> list[int]:
                return []

            def active_keys(self) -> list[int]:
                return []

            def grab(self) -> None:
                return

        created_tasks: list[asyncio.Task[None]] = []
        original_create_task = asyncio.create_task

        async def fake_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        def fake_create_task(coro):
            coro.close()
            task = original_create_task(asyncio.sleep(0))
            created_tasks.append(task)
            return task

        monkeypatch.setattr(gdm.evdev, "InputDevice", lambda _path: _FakeInputDevice())
        monkeypatch.setattr(gdm.evdev, "UInput", FakeUInput)
        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(gdm.asyncio, "create_task", fake_create_task)

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="045e:02a1",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.GAMEPAD,
            device_types=["gamepad"],
            gamepad_uinput=FakeUInput(),  # type: ignore[arg-type]
        )

        await device.grab()
        await asyncio.sleep(0)

        assert isinstance(device.uinput, FakeUInput)
        assert device.uinput.kwargs["name"] == "Xbox 360 Wireless Controller"
        assert device.uinput.kwargs["vendor"] == 0x045E
        assert device.uinput.kwargs["product"] == 0x02A1
        assert device.uinput.kwargs["version"] == 0x0114
        assert device.uinput.kwargs["bustype"] == 0x0003

        for task in created_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_grab_ignores_invalid_passthrough_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")
        monkeypatch.setattr(
            gdm,
            "uinput_identity",
            lambda *_args, **_kwargs: ("Bad Keyboard", 0x1234, 0x1_0000),
        )

        class _FakeInputDevice:
            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_L],
                    evdev.ecodes.EV_SYN: [],
                }

            def active_keys(self) -> list[int]:
                return []

            def grab(self) -> None:
                return

        created_tasks: list[asyncio.Task[None]] = []
        created_kwargs: list[dict[str, object]] = []
        original_create_task = asyncio.create_task

        async def fake_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        def fake_create_task(coro):
            coro.close()
            task = original_create_task(asyncio.sleep(0))
            created_tasks.append(task)
            return task

        def fake_uinput(*_args, **kwargs) -> FakeUInput:
            created_kwargs.append(kwargs)
            return FakeUInput(**kwargs)

        monkeypatch.setattr(gdm.evdev, "InputDevice", lambda _path: _FakeInputDevice())
        monkeypatch.setattr(gdm.evdev, "UInput", fake_uinput)
        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(gdm.asyncio, "create_task", fake_create_task)

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=FakeUInput(),  # type: ignore[arg-type]
        )

        await device.grab()
        await asyncio.sleep(0)

        assert created_kwargs
        assert created_kwargs[0]["name"] == "Bad Keyboard"
        assert "vendor" not in created_kwargs[0]
        assert "product" not in created_kwargs[0]

        for task in created_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_gamepad_passthrough_falls_back_when_identity_is_invalid(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "js")
        monkeypatch.setattr(
            gdm,
            "uinput_identity",
            lambda *_args, **_kwargs: ("Bad Gamepad", "nope", 0x1_0000),
        )

        class _FakeInputDevice:
            name = "Xbox 360 Wireless Controller"
            info = SimpleNamespace(
                vendor=0x045E,
                product=0x02A1,
                version=0x0114,
                bustype=0x0003,
            )

            def capabilities(self) -> dict[int, list[object]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
                    evdev.ecodes.EV_ABS: [
                        (
                            evdev.ecodes.ABS_X,
                            evdev.AbsInfo(0, -32768, 32767, 16, 128, 0),
                        )
                    ],
                    evdev.ecodes.EV_SYN: [],
                }

            def input_props(self) -> list[int]:
                return []

            def active_keys(self) -> list[int]:
                return []

            def grab(self) -> None:
                return

        created_tasks: list[asyncio.Task[None]] = []
        original_create_task = asyncio.create_task

        async def fake_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        def fake_create_task(coro):
            coro.close()
            task = original_create_task(asyncio.sleep(0))
            created_tasks.append(task)
            return task

        monkeypatch.setattr(gdm.evdev, "InputDevice", lambda _path: _FakeInputDevice())
        monkeypatch.setattr(gdm.evdev, "UInput", FakeUInput)
        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(gdm.asyncio, "create_task", fake_create_task)

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="045e:02a1",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.GAMEPAD,
            device_types=["gamepad"],
            gamepad_uinput=FakeUInput(),  # type: ignore[arg-type]
        )

        await device.grab()
        await asyncio.sleep(0)

        assert isinstance(device.uinput, FakeUInput)
        assert device.uinput.kwargs["name"] == "Bad Gamepad"
        assert device.uinput.kwargs["vendor"] == 0x045E
        assert device.uinput.kwargs["product"] == 0x02A1
        assert device.uinput.kwargs["version"] == 0x0114
        assert device.uinput.kwargs["bustype"] == 0x0003

        for task in created_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_gamepad_grab_hides_source_and_release_restores_stored_names(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "js")
        events: list[str] = []

        class _FakeInputDevice:
            name = "Xbox 360 Wireless Controller"
            info = SimpleNamespace(
                vendor=0x045E,
                product=0x02A1,
                version=0x0114,
                bustype=0x0003,
            )

            def capabilities(self) -> dict[int, list[object]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
                    evdev.ecodes.EV_ABS: [
                        (
                            evdev.ecodes.ABS_X,
                            evdev.AbsInfo(0, -32768, 32767, 16, 128, 0),
                        )
                    ],
                    evdev.ecodes.EV_SYN: [],
                }

            def input_props(self) -> list[int]:
                return []

            def active_keys(self) -> list[int]:
                return []

            def grab(self) -> None:
                events.append("grab")

            def ungrab(self) -> None:
                events.append("ungrab")

            def close(self) -> None:
                events.append("input-close")

        class _RecordingUInput(FakeUInput):
            def close(self) -> None:
                events.append("uinput-close")

        created_tasks: list[asyncio.Task[None]] = []
        original_create_task = asyncio.create_task

        async def fake_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        def fake_create_task(coro):
            coro.close()
            task = original_create_task(asyncio.sleep(60))
            created_tasks.append(task)
            return task

        async def fake_hide_source(path: str) -> list[str]:
            events.append(f"hide:{path}")
            return ["event22", "js0"]

        async def fake_restore_source(names: list[str]) -> None:
            events.append(f"restore:{','.join(names)}")

        monkeypatch.setattr(gdm.evdev, "InputDevice", lambda _path: _FakeInputDevice())
        monkeypatch.setattr(gdm.evdev, "UInput", _RecordingUInput)
        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(gdm.asyncio, "create_task", fake_create_task)
        monkeypatch.setattr(gdm.source_hiding, "hide_source", fake_hide_source)
        monkeypatch.setattr(
            gdm.source_hiding,
            "restore_source_by_kernel_names",
            fake_restore_source,
        )

        device = GrabbedDevice(
            path="/dev/input/event22",
            hardware_id="045e:02a1",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.GAMEPAD,
            device_types=["gamepad"],
            gamepad_uinput=FakeUInput(),  # type: ignore[arg-type]
        )

        await device.grab()
        await device.release()

        assert "hide:/dev/input/event22" in events
        assert device.source_hidden_kernel_names == []
        assert events.index("restore:event22,js0") > events.index("uinput-close")
        assert events == [
            "grab",
            "hide:/dev/input/event22",
            "ungrab",
            "input-close",
            "uinput-close",
            "restore:event22,js0",
        ]

        for task in created_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_cancelled_gamepad_hide_closes_without_speculative_restore(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "js")
        events: list[str] = []

        class _FakeInputDevice:
            name = "Xbox 360 Wireless Controller"
            info = SimpleNamespace(
                vendor=0x045E,
                product=0x02A1,
                version=0x0114,
                bustype=0x0003,
            )

            def capabilities(self) -> dict[int, list[object]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
                    evdev.ecodes.EV_ABS: [
                        (
                            evdev.ecodes.ABS_X,
                            evdev.AbsInfo(0, -32768, 32767, 16, 128, 0),
                        )
                    ],
                    evdev.ecodes.EV_SYN: [],
                }

            def input_props(self) -> list[int]:
                return []

            def active_keys(self) -> list[int]:
                return []

            def grab(self) -> None:
                events.append("grab")

            def close(self) -> None:
                events.append("input-close")

        class _RecordingUInput(FakeUInput):
            def close(self) -> None:
                events.append("uinput-close")

        async def fake_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        async def fake_hide_source(path: str) -> list[str]:
            events.append(f"hide:{path}")
            raise asyncio.CancelledError()

        async def fake_restore_source(names: list[str]) -> None:
            events.append(f"restore:{','.join(names)}")

        monkeypatch.setattr(gdm.evdev, "InputDevice", lambda _path: _FakeInputDevice())
        monkeypatch.setattr(gdm.evdev, "UInput", _RecordingUInput)
        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(gdm.source_hiding, "hide_source", fake_hide_source)
        monkeypatch.setattr(
            gdm.source_hiding,
            "restore_source_by_kernel_names",
            fake_restore_source,
        )

        device = GrabbedDevice(
            path="/dev/input/event22",
            hardware_id="045e:02a1",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.GAMEPAD,
            device_types=["gamepad"],
            gamepad_uinput=FakeUInput(),  # type: ignore[arg-type]
        )

        with pytest.raises(asyncio.CancelledError):
            await device.grab()

        assert device.source_hidden_kernel_names == []
        assert events == [
            "grab",
            "hide:/dev/input/event22",
            "uinput-close",
            "input-close",
        ]

    @pytest.mark.asyncio
    async def test_keyboard_grab_does_not_hide_source(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")
        hide_calls: list[str] = []
        restore_calls: list[list[str]] = []

        class _FakeInputDevice:
            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_L],
                    evdev.ecodes.EV_SYN: [],
                }

            def active_keys(self) -> list[int]:
                return []

            def grab(self) -> None:
                return

            def ungrab(self) -> None:
                return

            def close(self) -> None:
                return

        created_tasks: list[asyncio.Task[None]] = []
        original_create_task = asyncio.create_task

        async def fake_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        def fake_create_task(coro):
            coro.close()
            task = original_create_task(asyncio.sleep(60))
            created_tasks.append(task)
            return task

        async def fake_hide_source(path: str) -> list[str]:
            hide_calls.append(path)
            return ["event22"]

        async def fake_restore_source(names: list[str]) -> None:
            restore_calls.append(list(names))

        monkeypatch.setattr(gdm.evdev, "InputDevice", lambda _path: _FakeInputDevice())
        monkeypatch.setattr(gdm.evdev, "UInput", FakeUInput)
        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(gdm.asyncio, "create_task", fake_create_task)
        monkeypatch.setattr(gdm.source_hiding, "hide_source", fake_hide_source)
        monkeypatch.setattr(
            gdm.source_hiding,
            "restore_source_by_kernel_names",
            fake_restore_source,
        )

        device = GrabbedDevice(
            path="/dev/input/event12",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=FakeUInput(),  # type: ignore[arg-type]
        )

        await device.grab()
        await device.release()

        assert hide_calls == []
        assert restore_calls == []

        for task in created_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def test_gamepad_passthrough_fallback_name_includes_interface(self) -> None:
        device = SimpleNamespace(name="")

        assert (
            gdm._passthrough_name(  # pyright: ignore[reportPrivateUsage]
                device,  # type: ignore[arg-type]
                "045e:02a1",
                "js0",
                is_gamepad=True,
            )
            == "Keymasq Gamepad Passthrough (js0)"
        )

    def test_gamepad_passthrough_fallback_name_uses_hardware_id_without_interface(
        self,
    ) -> None:
        device = SimpleNamespace(name=None)

        assert (
            gdm._passthrough_name(  # pyright: ignore[reportPrivateUsage]
                device,  # type: ignore[arg-type]
                "045e:02a1",
                "",
                is_gamepad=True,
            )
            == "Keymasq Gamepad Passthrough (045e:02a1)"
        )

    def test_passthrough_input_id_parses_numbered_hardware_id(self) -> None:
        assert gdm._hardware_id_vendor_product(  # pyright: ignore[reportPrivateUsage]
            "045e:02a1@2"
        ) == (0x045E, 0x02A1)

    def test_passthrough_uinput_identity_is_bounded_for_opaque_hardware_ids(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        hardware_id = (
            "045e:02a1@/dev/input/by-id/"
            "usb-\u00a9Microsoft_Xbox_360_Wireless_Receiver_for_Windows_"
            "FD161BB0-if02-event-joystick"
        )
        monkeypatch.delenv("KEYMASQ_TEST_UINPUT", raising=False)

        normal_name, normal_vendor, normal_product = ldm.runtime_outputs.uinput_identity(
            f"keymasq-{hardware_id}",
            "passthrough",
            test_name=f"passthrough-{hardware_id}",
        )

        assert len(normal_name.encode("utf-8")) <= ldm.runtime_outputs.UINPUT_NAME_MAX_BYTES
        assert normal_name.startswith("keymasq-")
        assert "dev-input-by-id" not in normal_name
        assert normal_name != f"keymasq-{hardware_id}"
        assert normal_vendor is None
        assert normal_product is None

        monkeypatch.setenv("KEYMASQ_TEST_UINPUT", "1")
        test_name, test_vendor, test_product = ldm.runtime_outputs.uinput_identity(
            f"keymasq-{hardware_id}",
            "passthrough",
            test_name=f"passthrough-{hardware_id}",
        )

        assert len(test_name.encode("utf-8")) <= ldm.runtime_outputs.UINPUT_NAME_MAX_BYTES
        assert test_name.startswith("keymasq-test-")
        assert "dev-input-by-id" not in test_name
        assert test_name != f"keymasq-test-passthrough-{hardware_id}"
        assert test_vendor == 0x4B46
        assert test_product == 0x1004

    @pytest.mark.asyncio
    async def test_rapidfire_key_releases_before_exiting_when_stopped_during_hold(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        fake_uinput = FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=fake_uinput,  # type: ignore[arg-type]
        )
        device._running = True
        device.state.rapidfire_active["btn_side"] = True

        async def fake_sleep(_delay: float) -> None:
            device.state.rapidfire_active["btn_side"] = False

        monkeypatch.setattr(gdm.asyncio, "sleep", fake_sleep)

        await gdr.rapidfire_key(
            device,
            evdev.ecodes.KEY_A,
            50,
            50,
            "btn_side",
            fake_uinput,  # type: ignore[arg-type]
            asyncio_mod=gdm.ASYNCIO_RUNTIME,
        )

        assert fake_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]

    @pytest.mark.asyncio
    async def test_start_rapidfire_task_stops_existing_state_before_creating_new_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        fake_uinput = FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=fake_uinput,  # type: ignore[arg-type]
        )

        calls: list[str] = []
        original_stop = gdr.stop_rapidfire

        def wrapped_stop(device_runtime: GrabbedDevice, event_name: str) -> None:
            calls.append(f"stop:{event_name}")
            original_stop(device_runtime, event_name)

        monkeypatch.setattr(gdr, "stop_rapidfire", wrapped_stop)

        def task_factory() -> asyncio.Task:
            calls.append("factory")
            return asyncio.create_task(asyncio.sleep(0))

        gdr.start_rapidfire_task(
            device,
            "btn_side",
            "key",
            task_factory,
            code=evdev.ecodes.KEY_A,
            uinput=fake_uinput,  # type: ignore[arg-type]
            axis_code=None,
        )
        await asyncio.sleep(0)
        gdr.stop_rapidfire(device, "btn_side")

        assert calls[:2] == ["stop:btn_side", "factory"]

    @pytest.mark.asyncio
    async def test_rapidfire_quick_release_and_repress_does_not_leave_task_or_key_stuck(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        fake_uinput = FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"btn_side": "btn_side"},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.MOUSE,
            keyboard_uinput=fake_uinput,  # type: ignore[arg-type]
        )
        device._running = True

        action = dm.MappingAction(
            action_type=ActionType.KEYBOARD,
            target="key_a",
            rapidfire_enabled=True,
            rapidfire_hold_ms=50,
            rapidfire_wait_ms=50,
        )
        press_event = SimpleNamespace(value=1)
        release_event = SimpleNamespace(value=0)

        await gda.execute_action(
            device,
            action,
            press_event,
            "btn_side",
            deps=gde.build_action_execution_deps(fire_and_observe_fn=gde._fire_and_observe),
        )
        await gda.execute_action(
            device,
            action,
            release_event,
            "btn_side",
            deps=gde.build_action_execution_deps(fire_and_observe_fn=gde._fire_and_observe),
        )
        await gda.execute_action(
            device,
            action,
            press_event,
            "btn_side",
            deps=gde.build_action_execution_deps(fire_and_observe_fn=gde._fire_and_observe),
        )
        await asyncio.sleep(0)

        assert len(device.state.rapidfire_tasks) == 1

        await gda.execute_action(
            device,
            action,
            release_event,
            "btn_side",
            deps=gde.build_action_execution_deps(fire_and_observe_fn=gde._fire_and_observe),
        )
        await asyncio.sleep(0.01)

        assert device.state.rapidfire_tasks == {}
        assert device.state.rapidfire_outputs == {}
        assert device.state.held_output_keys["keyboard"] == set()
        assert fake_uinput.writes[-1] == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_A,
            0,
        )

    @pytest.mark.asyncio
    async def test_combo_passthrough_release_still_stops_active_rapidfire(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "mouse")

        decisions = [
            None,
            ComboDecision(passthrough_current_event=True),
        ]

        async def event_callback(*_args):
            return decisions.pop(0)

        fake_uinput = FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"btn_side": "btn_side"},
            mapping_getter=lambda: {
                "btn_side": dm.MappingAction(
                    action_type=ActionType.KEYBOARD,
                    target="key_a",
                    rapidfire_enabled=True,
                    rapidfire_hold_ms=50,
                    rapidfire_wait_ms=50,
                )
            },
            event_callback=event_callback,
            device_type=DeviceType.MOUSE,
            keyboard_uinput=fake_uinput,  # type: ignore[arg-type]
        )
        device._running = True

        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_SIDE,
            value=1,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_SIDE,
            value=0,
        )

        await gde.process_event(device, press_event, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)
        assert "btn_side" in device.state.held_source_actions

        await gde.process_event(device, release_event, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0.01)

        assert device.state.rapidfire_tasks == {}
        assert device.state.rapidfire_outputs == {}
        assert device.state.held_output_keys["keyboard"] == set()
        assert "btn_side" not in device.state.held_source_actions
        assert fake_uinput.writes[-1] == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_A,
            0,
        )

    @pytest.mark.asyncio
    async def test_combo_passthrough_keydown_forces_matching_passthrough_keyup(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        decisions = [
            ComboDecision(passthrough_current_event=True, reset_candidates=True),
            None,
        ]

        async def event_callback(*_args):
            return decisions.pop(0)

        passthrough_uinput = FakeUInput()
        mapped_uinput = FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_1": "key_1"},
            mapping_getter=lambda: {},
            event_callback=event_callback,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=mapped_uinput,  # type: ignore[arg-type]
        )
        device._running = True
        device.uinput = passthrough_uinput  # type: ignore[assignment]

        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_1,
            value=1,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_1,
            value=0,
        )

        await gde.process_event(device, press_event, deps=grabbed_event_processing_deps())
        await gde.process_event(device, release_event, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)

        assert passthrough_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 0),
        ]
        assert mapped_uinput.writes == []
        assert device.state.combo_passthrough_held == set()

    @pytest.mark.asyncio
    async def test_combo_passthrough_does_not_bypass_unrelated_mapping_action(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        decisions = [
            ComboDecision(passthrough_current_event=True, reset_candidates=True),
            ComboDecision(passthrough_current_event=True, reset_candidates=True),
        ]

        async def event_callback(*_args):
            return decisions.pop(0)

        passthrough_uinput = FakeUInput()
        mapped_uinput = FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_1": "key_1"},
            mapping_getter=lambda: {
                "key_1": dm.MappingAction(
                    action_type=ActionType.KEYBOARD,
                    target="key_b",
                )
            },
            event_callback=event_callback,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=mapped_uinput,  # type: ignore[arg-type]
        )
        device._running = True
        device.uinput = passthrough_uinput  # type: ignore[assignment]

        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_1,
            value=1,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_1,
            value=0,
        )

        await gde.process_event(device, press_event, deps=grabbed_event_processing_deps())
        await gde.process_event(device, release_event, deps=grabbed_event_processing_deps())

        assert passthrough_uinput.writes == []
        assert mapped_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
        ]
        assert device.state.combo_passthrough_held == set()

    @pytest.mark.asyncio
    async def test_combo_consumed_modifier_release_still_passthroughs_when_held(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        decisions = [
            ComboDecision(passthrough_current_event=True),
            ComboDecision(consume_current_event=True),
        ]

        async def event_callback(*_args):
            return decisions.pop(0)

        passthrough_uinput = FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_leftalt": "key_leftalt"},
            mapping_getter=lambda: {},
            event_callback=event_callback,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=FakeUInput(),  # type: ignore[arg-type]
        )
        device._running = True
        device.uinput = passthrough_uinput  # type: ignore[assignment]

        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_LEFTALT,
            value=1,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_LEFTALT,
            value=0,
        )

        await gde.process_event(device, press_event, deps=grabbed_event_processing_deps())
        await gde.process_event(device, release_event, deps=grabbed_event_processing_deps())

        assert passthrough_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTALT, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTALT, 0),
        ]
        assert device.state.combo_passthrough_held == set()

    @pytest.mark.asyncio
    async def test_combo_consumed_release_still_stops_existing_rapidfire_action(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        decisions = [
            None,
            ComboDecision(consume_current_event=True),
        ]

        async def event_callback(*_args):
            return decisions.pop(0)

        fake_uinput = FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_f5": "key_f5"},
            mapping_getter=lambda: {
                "key_f5": dm.MappingAction(
                    action_type=ActionType.KEYBOARD,
                    target="key_b",
                    rapidfire_enabled=True,
                    rapidfire_hold_ms=50,
                    rapidfire_wait_ms=50,
                )
            },
            event_callback=event_callback,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=fake_uinput,  # type: ignore[arg-type]
        )
        device._running = True

        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_F5,
            value=1,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_F5,
            value=0,
        )

        await gde.process_event(device, press_event, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)
        assert "key_f5" in device.state.held_source_actions

        await gde.process_event(device, release_event, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0.01)

        assert device.state.rapidfire_tasks == {}
        assert device.state.rapidfire_outputs == {}
        assert device.state.held_output_keys["keyboard"] == set()
        assert "key_f5" not in device.state.held_source_actions
        assert fake_uinput.writes[-1] == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_B,
            0,
        )

    @pytest.mark.asyncio
    async def test_release_interface_skipped_when_path_still_desired(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        manager = DeviceManager(release_grace_s=0.001)
        fake_device = type("Device", (), {})()
        fake_device.release = AsyncMock()

        async def release_interface(_hardware_id: str, _path: str) -> None:
            await fake_device.release()

        manager.grabbed_devices = {"hw": []}
        manager.grab_state.desired_paths["hw"] = {"/dev/input/event0"}
        monkeypatch.setattr(ldm, "release_interface_unlocked", release_interface)

        await ldm.delayed_interface_release(
            manager,
            "hw",
            "/dev/input/event0",
            0.001,
            asyncio_mod=ldm.ASYNCIO_RUNTIME,
        )

        fake_device.release.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_release_interface_clears_scoped_combo_runtime_before_device_teardown(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        fake_device = SimpleNamespace(
            path="/dev/input/event0",
            interface_id="mouse",
            release_tracked_outputs=Mock(),
            release=AsyncMock(),
        )
        manager.grabbed_devices = {"hw": [fake_device]}
        clear_combo_scope = AsyncMock()
        clear_combo_runtime = AsyncMock()
        monkeypatch.setattr(cdm, "clear_combo_runtime_for_binding_scope", clear_combo_scope)
        monkeypatch.setattr(cdm, "clear_combo_runtime", clear_combo_runtime)

        await ldm.release_interface_unlocked(manager, "hw", "/dev/input/event0")

        clear_combo_scope.assert_awaited_once()
        clear_combo_runtime.assert_not_awaited()
        fake_device.release_tracked_outputs.assert_called_once()
        fake_device.release.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_release_interface_preserves_desired_state_for_missing_managed_device(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        fake_device = SimpleNamespace(
            path="/dev/input/event0",
            interface_id="mouse",
            release_tracked_outputs=Mock(),
            release=AsyncMock(),
        )
        action = MappingAction(action_type=ActionType.KEYBOARD, target="key_a")
        manager.grabbed_devices = {"hw": [fake_device]}
        manager.active_mappings = {"hw": {"btn_side": action}}
        manager.grab_state.desired_paths["hw"] = {"/dev/input/event0"}
        manager.grab_state.desired_grabs["hw"] = DesiredGrabConfig(
            paths={"/dev/input/event0"},
            button_map={"btn_side": "btn_side"},
        )
        monkeypatch.setattr(cdm, "clear_combo_runtime_for_binding_scope", AsyncMock())

        await ldm.release_interface_unlocked(manager, "hw", "/dev/input/event0")

        assert manager.grabbed_devices == {}
        assert manager.active_mappings["hw"] == {"btn_side": action}
        assert manager.grab_state.desired_paths["hw"] == {"/dev/input/event0"}
        assert manager.grab_state.desired_grabs["hw"].paths == {"/dev/input/event0"}

    @pytest.mark.asyncio
    async def test_release_device_clears_scoped_combo_runtime_before_releasing_hardware(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        fake_device = SimpleNamespace(release=AsyncMock())
        manager.grabbed_devices = {"hw": [fake_device]}
        clear_combo_scope = AsyncMock()
        clear_combo_runtime = AsyncMock()
        destroy_global_uinputs = Mock()
        monkeypatch.setattr(cdm, "clear_combo_runtime_for_binding_scope", clear_combo_scope)
        monkeypatch.setattr(cdm, "clear_combo_runtime", clear_combo_runtime)
        monkeypatch.setattr(ldm.runtime_outputs, "destroy_global_uinputs", destroy_global_uinputs)

        result = await ldm.release_device_unlocked(manager, "hw", log=dm.log)

        assert result == {"released": True, "hardware_id": "hw"}
        clear_combo_scope.assert_awaited_once()
        clear_combo_runtime.assert_not_awaited()
        fake_device.release.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_combo_runtime_for_binding_scope_stops_only_affected_actions(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        manager.combo_state.engine.drop_candidates_for_binding_scope = Mock(  # type: ignore[method-assign]
            return_value={"combo-1"}
        )
        stop_combo_action = AsyncMock()
        refresh_combo_timeout_watchdog = Mock()
        monkeypatch.setattr(cdm, "stop_combo_action", stop_combo_action)
        monkeypatch.setattr(cdm, "refresh_combo_timeout_watchdog", refresh_combo_timeout_watchdog)

        await cdm.clear_combo_runtime_for_binding_scope(
            manager, "1234:5678", "mouse", deps=combo_runtime_deps()
        )

        manager.combo_state.engine.drop_candidates_for_binding_scope.assert_called_once_with(
            "1234:5678",
            "mouse",
        )
        stop_combo_action.assert_awaited_once()
        refresh_combo_timeout_watchdog.assert_called_once()
