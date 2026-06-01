import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping
from typing import Final, Protocol, TypeVar, cast

import evdev

_T = TypeVar("_T")


class WritableUInput(Protocol):
    def write(self, event_type: int, code: int, value: int) -> None: ...

    def syn(self) -> None: ...

    def close(self) -> None: ...


type UInputWriter = Callable[[object | None], WritableUInput | None]


class AsyncioEvent(Protocol):
    def set(self) -> None: ...

    async def wait(self) -> object: ...


class AsyncioLoop(Protocol):
    def add_reader(self, fd: int, callback: Callable[[], object], /) -> None: ...

    def remove_reader(self, fd: int) -> bool: ...


class AsyncioRuntimeAdapter:
    CancelledError = asyncio.CancelledError
    TimeoutError = asyncio.TimeoutError

    async def sleep(self, delay: float, /) -> None:
        await asyncio.sleep(delay)

    def create_task(self, coro: Awaitable[_T], /) -> asyncio.Task[_T]:
        return asyncio.ensure_future(coro)

    def current_task(self) -> asyncio.Task[None] | None:
        return cast(asyncio.Task[None] | None, asyncio.current_task())

    def to_thread(
        self,
        func: Callable[..., _T],
        /,
        *args: object,
        **kwargs: object,
    ) -> Awaitable[_T]:
        return asyncio.to_thread(func, *args, **kwargs)

    def get_running_loop(self) -> AsyncioLoop:
        return cast(AsyncioLoop, asyncio.get_running_loop())

    def gather(
        self, *aws: Awaitable[object], return_exceptions: bool = False
    ) -> Awaitable[object]:
        return cast(
            Awaitable[object],
            asyncio.gather(*aws, return_exceptions=return_exceptions),
        )

    def wait_for(self, aw: Awaitable[_T], timeout: float) -> Awaitable[_T]:
        return asyncio.wait_for(aw, timeout)

    def create_event(self) -> AsyncioEvent:
        return cast(AsyncioEvent, asyncio.Event())


ASYNCIO_RUNTIME: Final[AsyncioRuntimeAdapter] = AsyncioRuntimeAdapter()


def identity_uinput_writer(device: object | None) -> WritableUInput | None:
    return cast(WritableUInput | None, device)


def close_device(device: object) -> None:
    """Close evdev-style devices synchronously; this is intended as fd-close cleanup."""
    close = getattr(device, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        pass


class _ComboEcodesByType(Mapping[int, Mapping[int, object]]):
    def __getitem__(self, key: int) -> Mapping[int, object]:
        return cast(Mapping[int, object], evdev.ecodes.bytype[int(key)])

    def __iter__(self) -> Iterator[int]:
        return iter(evdev.ecodes.bytype)

    def __len__(self) -> int:
        return len(evdev.ecodes.bytype)


class _ComboEcodes:
    EV_KEY: Final[int] = evdev.ecodes.EV_KEY
    EV_REL: Final[int] = evdev.ecodes.EV_REL
    EV_SYN: Final[int] = evdev.ecodes.EV_SYN
    EV_ABS: Final[int] = evdev.ecodes.EV_ABS
    REL_X: Final[int] = evdev.ecodes.REL_X
    REL_Y: Final[int] = evdev.ecodes.REL_Y
    REL_WHEEL: Final[int] = evdev.ecodes.REL_WHEEL
    REL_HWHEEL: Final[int] = evdev.ecodes.REL_HWHEEL
    ABS_Z: Final[int] = evdev.ecodes.ABS_Z
    ABS_RZ: Final[int] = evdev.ecodes.ABS_RZ
    bytype: Final[_ComboEcodesByType] = _ComboEcodesByType()


class ComboEvdevAdapter:
    ecodes: Final[_ComboEcodes] = _ComboEcodes()


COMBO_EVDEV_RUNTIME: Final[ComboEvdevAdapter] = ComboEvdevAdapter()
