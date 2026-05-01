from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import evdev

from keymasq.keymasqd.output_helpers import parse_mouse_output_target


class _WritableUInput(Protocol):
    def write(self, event_type: int, code: int, value: int) -> None: ...

    def syn(self) -> None: ...


class _AsyncioModule(Protocol):
    async def sleep(self, delay: float, /) -> None: ...


type UInputWriter = Callable[[object | None], _WritableUInput | None]
type RelativePulseEmitter = Callable[[], None]
type RelativePulseActive = Callable[[], bool]


@dataclass(frozen=True)
class MouseOutputTarget:
    event_type: int
    code: int
    relative_value: int = 0

    @property
    def is_relative(self) -> bool:
        return int(self.event_type) == evdev.ecodes.EV_REL


def resolve_mouse_output_target(target: str | None) -> MouseOutputTarget | None:
    event_type, code, relative_value = parse_mouse_output_target(target)
    if event_type is None or code is None:
        return None
    return MouseOutputTarget(
        event_type=int(event_type),
        code=int(code),
        relative_value=int(relative_value),
    )


_HI_RES_SCROLL = (
    (getattr(evdev.ecodes, "REL_WHEEL_HI_RES", None), evdev.ecodes.REL_WHEEL),
    (getattr(evdev.ecodes, "REL_HWHEEL_HI_RES", None), evdev.ecodes.REL_HWHEEL),
)


def emit_relative_pulse(
    uinput: _WritableUInput | None,
    code: int,
    value: int,
    *,
    ev_rel_code: int,
) -> None:
    if uinput is None:
        return
    uinput.write(int(ev_rel_code), int(code), int(value))

    for hi_res_code, low_res_code in _HI_RES_SCROLL:
        if hi_res_code is not None and int(code) == int(low_res_code):
            uinput.write(int(ev_rel_code), int(hi_res_code), int(value) * 120)
            break

    uinput.syn()


def write_relative_pulse(
    uinput_dev: object | None,
    code: int,
    value: int,
    *,
    ev_rel_code: int,
    uinput_writer: UInputWriter,
) -> None:
    emit_relative_pulse(
        uinput_writer(uinput_dev),
        code,
        value,
        ev_rel_code=ev_rel_code,
    )


async def tap_relative_pulse(
    *,
    emit_pulse: RelativePulseEmitter,
    hold_s: float,
    asyncio_mod: _AsyncioModule,
) -> None:
    emit_pulse()
    await asyncio_mod.sleep(hold_s)


async def rapidfire_relative_pulses(
    *,
    emit_pulse: RelativePulseEmitter,
    is_active: RelativePulseActive,
    hold_s: float,
    wait_s: float,
    asyncio_mod: _AsyncioModule,
) -> None:
    while is_active():
        emit_pulse()
        await asyncio_mod.sleep(hold_s)
        if not is_active():
            break
        await asyncio_mod.sleep(wait_s)
