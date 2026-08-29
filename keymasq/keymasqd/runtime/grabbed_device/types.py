import asyncio
import logging
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Protocol, TypeVar

import evdev

from keymasq.common.ipc import CommandType
from keymasq.common.model.actions import MappingAction
from keymasq.keymasqd.combo_engine import ComboDecision
from keymasq.keymasqd.recording import RecordingManager
from keymasq.keymasqd.runtime.adapters import (
    AsyncioEvent,
    AsyncioLoop,
    DeviceInfo,
    UInputWriter,
)
from keymasq.keymasqd.runtime.repeat import RepeatRuntimeState

if TYPE_CHECKING:
    from keymasq.keymasqd.superkey_state import SuperkeyMachine

type BroadcastCallback = Callable[[CommandType, dict[str, object]], Awaitable[None]]
type CursorPositionSetter = Callable[[int, int], Awaitable[dict[str, object]]]
type NaturalMouseMover = Callable[
    [int, int, float, float, str, int, int],
    Awaitable[dict[str, object]],
]
type MappingGetter = Callable[[], dict[str, MappingAction]]
type DeviceEventCallback = Callable[..., Awaitable[ComboDecision | bool | None]]
type MacroPlayer = Callable[..., Awaitable[dict[str, object]]]
type EmergencyResetter = Callable[[], Awaitable[dict[str, object]]]
type DeviceInspectorEventCallback = Callable[[dict[str, object]], None]
type DeviceInspectorActiveGetter = Callable[[str], bool]
type DeviceInspectorSuppressionGetter = Callable[[str], bool]
type DeviceInspectorSuppressedIdsGetter = Callable[[], set[str]]
type DeviceInspectorSuppressionDisabler = Callable[[str, str], Awaitable[dict[str, object]]]
type ProfileActivationRecorder = Callable[[str | None, str | None], None]
type ProfileActivationTriggerObserver = Callable[[str | None], None]
type FireAndObserve = Callable[[Awaitable[object], str], asyncio.Task[object]]
type RuntimeCleanupCallback = Callable[[str, str | None], Awaitable[None]]
type RuntimeDisconnectCallback = Callable[[str, str], Awaitable[None]]
type OutputTracker = Callable[[str, int, int], bool]
_T = TypeVar("_T")


class InputEventLike(Protocol):
    type: int
    code: int
    value: int


class ManagedInputDevice(Protocol):
    path: str
    name: str | None
    info: DeviceInfo

    def grab(self) -> None: ...

    def ungrab(self) -> None: ...

    def capabilities(self) -> dict[int, Sequence[object]]: ...

    def async_read_loop(self) -> AsyncIterator[evdev.InputEvent]: ...

    def fileno(self) -> int: ...

    def read_one(self) -> evdev.InputEvent | None: ...

    def active_keys(self) -> Sequence[int]: ...

    def absinfo(self, code: int) -> object: ...

    def input_props(self) -> Iterable[int]: ...

    def close(self) -> None: ...


type TaskFactory = Callable[[], asyncio.Task[None]]
type RelativePulseEmitter = Callable[[], None]
type RelativePulseActive = Callable[[], bool]


class AsyncioModule(Protocol):
    def get_running_loop(self) -> AsyncioLoop: ...

    def create_event(self) -> AsyncioEvent: ...

    def wait_for(self, aw: Awaitable[_T], timeout: float) -> Awaitable[_T]: ...

    async def sleep(self, delay: float, /) -> None: ...

    def current_task(self) -> asyncio.Task[object] | None: ...

    def create_task(self, coro: Coroutine[object, object, _T], /) -> asyncio.Task[_T]: ...

    def to_thread(
        self,
        func: Callable[..., _T],
        /,
        *args: object,
        **kwargs: object,
    ) -> Awaitable[_T]: ...


class TimeModule(Protocol):
    def monotonic(self) -> float: ...

    def perf_counter_ns(self) -> int: ...


class Ecodes(Protocol):
    EV_KEY: Final[int]
    EV_REL: Final[int]
    EV_SYN: Final[int]
    EV_ABS: Final[int]
    REL_X: Final[int]
    REL_Y: Final[int]
    REL_WHEEL: Final[int]
    REL_HWHEEL: Final[int]
    ABS_Z: Final[int]
    ABS_RZ: Final[int]
    bytype: Final[Mapping[int, Mapping[int, object]]]


class EvdevModule(Protocol):
    ecodes: Final[Ecodes]


type ClassifyEventDeviceTypeFn = Callable[[evdev.InputEvent, list[str]], str]


@dataclass(frozen=True)
class ActionExecutionDeps:
    asyncio_mod: AsyncioModule
    fire_and_observe_fn: FireAndObserve
    evdev_mod: EvdevModule
    uinput_writer: UInputWriter


@dataclass(frozen=True)
class EventProcessingDeps:
    evdev_mod: EvdevModule
    time_mod: TimeModule
    log: logging.Logger
    classify_event_device_type_fn: ClassifyEventDeviceTypeFn
    action_deps: ActionExecutionDeps


@dataclass
class RapidfireOutputState:
    kind: str
    code: int | None = None
    uinput: object | None = None
    axis_code: int | None = None
    axis_release_value: int = 0
    bucket: str | None = None
    output_tracker: OutputTracker | None = None
    pressed: bool = False


@dataclass
class AnalogGamepadOutputState:
    output_id: str | None
    reset_axes: tuple[tuple[int, int], ...]


@dataclass
class GrabbedDeviceState:
    rapidfire_active: dict[str, bool] = field(default_factory=dict)
    rapidfire_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    rapidfire_outputs: dict[str, RapidfireOutputState] = field(default_factory=dict)
    tap_active: dict[str, bool] = field(default_factory=dict)
    superkey_machines: dict[str, "SuperkeyMachine"] = field(default_factory=dict)
    repeat_active_actions: dict[str, MappingAction] = field(default_factory=dict)
    passthrough_frame_output: object | None = None
    passthrough_abs_neutral_values: dict[int, int] = field(default_factory=dict)
    passthrough_mt_slot: int = 0
    passthrough_mt_active_slots: set[int] = field(default_factory=set)
    held_source_keys: set[str] = field(default_factory=set)
    combo_passthrough_held: set[str] = field(default_factory=set)
    combo_recalled_bindings: set[str] = field(default_factory=set)
    held_output_keys: dict[str, set[int]] = field(
        default_factory=lambda: {
            "passthrough": set(),
            "keyboard": set(),
            "mouse": set(),
            "gamepad": set(),
        }
    )
    held_output_abs: dict[str, set[int]] = field(
        default_factory=lambda: {
            "gamepad": set(),
        }
    )
    superkey_output_refcounts: dict[str, dict[int, int]] = field(
        default_factory=lambda: {
            "keyboard": {},
            "mouse": {},
            "gamepad": {},
        }
    )
    superkey_abs_refcounts: dict[str, dict[int, int]] = field(
        default_factory=lambda: {
            "gamepad": {},
        }
    )
    held_source_actions: dict[str, MappingAction | None] = field(default_factory=dict)
    held_profile_trigger_events: set[str] = field(default_factory=set)
    analog_axis_values: dict[str, dict[str, float]] = field(default_factory=dict)
    analog_active_thresholds: dict[str, set[str]] = field(default_factory=dict)
    analog_active_threshold_actions: dict[
        str,
        tuple[tuple[int, MappingAction], ...],
    ] = field(default_factory=dict)
    analog_threshold_output_refcounts: dict[str, dict[int, int]] = field(default_factory=dict)
    analog_threshold_abs_refcounts: dict[str, dict[int, int]] = field(default_factory=dict)
    analog_mouse_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    analog_mouse_accumulators: dict[str, tuple[float, float]] = field(default_factory=dict)
    analog_mouse_area_offsets: dict[str, tuple[float, float]] = field(default_factory=dict)
    analog_mouse_area_active: set[str] = field(default_factory=set)
    analog_gamepad_outputs: dict[str, AnalogGamepadOutputState] = field(default_factory=dict)


class ActionRuntime(Protocol):
    @property
    def path(self) -> str: ...

    @property
    def hardware_id(self) -> str: ...

    @property
    def uinput(self) -> object | None: ...

    @property
    def keyboard_uinput(self) -> object | None: ...

    @property
    def mouse_uinput(self) -> object | None: ...

    @property
    def gamepad_uinput(self) -> object | None: ...

    @property
    def broadcast_callback(self) -> BroadcastCallback | None: ...

    @property
    def cursor_position_setter(self) -> CursorPositionSetter | None: ...

    @property
    def natural_mouse_mover(self) -> NaturalMouseMover | None: ...

    @property
    def macro_player(self) -> MacroPlayer | None: ...

    @property
    def emergency_resetter(self) -> EmergencyResetter | None: ...

    @property
    def suppress_rel_getter(self) -> Callable[[], bool] | None: ...

    @property
    def state(self) -> GrabbedDeviceState: ...

    @property
    def repeat_state(self) -> RepeatRuntimeState | None: ...

    @property
    def running(self) -> bool: ...

    def resolve_gamepad_output(self, output_id: str | None, context: str) -> object | None: ...


class GrabbedDeviceRuntime(ActionRuntime, Protocol):
    @property
    def input_suspended(self) -> bool: ...

    @property
    def current_event_task(self) -> asyncio.Task[None] | None: ...

    @current_event_task.setter
    def current_event_task(self, task: asyncio.Task[None] | None) -> None: ...

    def fire_and_observe(self, coro: Awaitable[object], label: str) -> asyncio.Task[object]: ...

    @property
    def stable_path(self) -> str: ...

    @property
    def interface_id(self) -> str: ...

    @property
    def device_types(self) -> list[str]: ...

    @property
    def verbosity(self) -> int: ...

    @property
    def device(self) -> ManagedInputDevice | None: ...

    @property
    def recording_manager(self) -> RecordingManager | None: ...

    @property
    def inspector_event_callback(self) -> DeviceInspectorEventCallback | None: ...

    @property
    def inspector_active_getter(self) -> DeviceInspectorActiveGetter | None: ...

    @property
    def inspector_suppression_getter(self) -> DeviceInspectorSuppressionGetter | None: ...

    @property
    def inspector_suppressed_ids_getter(
        self,
    ) -> DeviceInspectorSuppressedIdsGetter | None: ...

    @property
    def inspector_suppression_disabler(
        self,
    ) -> DeviceInspectorSuppressionDisabler | None: ...

    @property
    def profile_activation_recorder(self) -> ProfileActivationRecorder | None: ...

    @property
    def profile_activation_trigger_start_observer(
        self,
    ) -> ProfileActivationTriggerObserver | None: ...

    @property
    def profile_activation_trigger_end_observer(
        self,
    ) -> ProfileActivationTriggerObserver | None: ...

    @property
    def diagnostics_recorder(self) -> Callable[[str, float], None] | None: ...

    @property
    def runtime_cleanup_callback(self) -> RuntimeCleanupCallback | None: ...

    @property
    def runtime_disconnect_callback(self) -> RuntimeDisconnectCallback | None: ...

    @property
    def mapping_getter(self) -> MappingGetter: ...

    @property
    def event_callback(self) -> DeviceEventCallback: ...

    @property
    def evdev_to_button(self) -> dict[str, str]: ...

    @property
    def event_binding_to_button(self) -> dict[tuple[int, int, int | None], str]: ...

    @property
    def event_code_to_button(self) -> dict[tuple[int, int], str]: ...

    @property
    def analog_inputs(self) -> dict[str, object]: ...

    @property
    def analog_axis_bindings(self) -> dict[tuple[int, int], tuple[str, str]]: ...

    @property
    def analog_axis_output_codes(self) -> dict[tuple[str, str], int]: ...

    @property
    def analog_axis_ranges(self) -> dict[tuple[str, str], tuple[int, int]]: ...

    @property
    def analog_axis_calibrations(self) -> dict[tuple[str, str], dict[str, object]]: ...

    async def reset_superkeys(self) -> None: ...

    async def reset_analog_controls(self) -> None: ...
