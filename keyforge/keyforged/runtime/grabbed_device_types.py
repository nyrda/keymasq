import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol, TypeVar

import evdev

from keyforge.common.ipc import CommandType
from keyforge.common.models import MappingAction
from keyforge.keyforged.combo_engine import ComboDecision
from keyforge.keyforged.recording import RecordingManager
from keyforge.keyforged.superkey_state import SuperkeyMachine

type BroadcastCallback = Callable[[CommandType, dict[str, object]], Awaitable[None]]
type MappingGetter = Callable[[], dict[str, MappingAction]]
type DeviceEventCallback = Callable[..., Awaitable[ComboDecision | bool | None]]
type MacroPlayer = Callable[..., Awaitable[dict[str, object]]]
type FireAndObserve = Callable[[Awaitable[object], str], asyncio.Task[object]]
type RuntimeCleanupCallback = Callable[[str, str | None], Awaitable[None]]
_T = TypeVar("_T")


class InputEventLike(Protocol):
    type: int
    code: int
    value: int


class DeviceInfo(Protocol):
    vendor: int
    product: int


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

    def close(self) -> None: ...


class WritableUInput(Protocol):
    def write(self, event_type: int, code: int, value: int) -> None: ...

    def syn(self) -> None: ...

    def close(self) -> None: ...


type UInputWriter = Callable[[object | None], WritableUInput | None]
type TaskFactory = Callable[[], asyncio.Task[None]]


class ErrnoModule(Protocol):
    EAGAIN: Final[int]
    EWOULDBLOCK: Final[int]


class AsyncioEvent(Protocol):
    def set(self) -> None: ...

    async def wait(self) -> object: ...


class AsyncioLoop(Protocol):
    def add_reader(self, fd: int, callback: Callable[[], object], /) -> None: ...

    def remove_reader(self, fd: int) -> bool: ...


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


class ContextlibModule(Protocol):
    def suppress(
        self, *exceptions: type[BaseException]
    ) -> contextlib.AbstractContextManager[None]: ...


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
    ABS_Z: Final[int]
    ABS_RZ: Final[int]
    bytype: Final[Mapping[int, Mapping[int, object]]]


class EvdevModule(Protocol):
    ecodes: Final[Ecodes]


type ClassifyEventDeviceTypeFn = Callable[[evdev.InputEvent, list[str]], str]


@dataclass
class RapidfireOutputState:
    kind: str
    code: int | None = None
    uinput: object | None = None
    axis_code: int | None = None


@dataclass
class GrabbedDeviceState:
    rapidfire_active: dict[str, bool] = field(default_factory=dict)
    rapidfire_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    rapidfire_outputs: dict[str, RapidfireOutputState] = field(default_factory=dict)
    tap_active: dict[str, bool] = field(default_factory=dict)
    superkey_machines: dict[str, SuperkeyMachine] = field(default_factory=dict)
    combo_passthrough_held: set[str] = field(default_factory=set)
    held_output_keys: dict[str, set[int]] = field(
        default_factory=lambda: {
            "passthrough": set(),
            "keyboard": set(),
            "mouse": set(),
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
    held_source_actions: dict[str, MappingAction | None] = field(default_factory=dict)


class GrabbedDeviceRuntime(Protocol):
    @property
    def path(self) -> str: ...

    @property
    def hardware_id(self) -> str: ...

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
    def recording_manager(self) -> RecordingManager | None: ...

    @property
    def macro_player(self) -> MacroPlayer | None: ...

    @property
    def suppress_rel_getter(self) -> Callable[[], bool] | None: ...

    @property
    def diagnostics_recorder(self) -> Callable[[str, float], None] | None: ...

    @property
    def runtime_cleanup_callback(self) -> RuntimeCleanupCallback | None: ...

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
    def state(self) -> GrabbedDeviceState: ...

    @property
    def _running(self) -> bool: ...

    async def reset_superkeys(self) -> None: ...


def runtime_is_running(device_runtime: GrabbedDeviceRuntime) -> bool:
    return device_runtime._running  # pyright: ignore[reportPrivateUsage]
