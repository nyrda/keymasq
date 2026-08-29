import asyncio
import ctypes
import errno
import logging
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol, cast

import evdev

from keymasq.keymasqd.runtime.adapters import ASYNCIO_RUNTIME, AsyncioRuntimeAdapter

# EV_FF passthrough writes to the grabbed physical gamepad node, which
# 99-keymasq-hide-grabbed.rules resets to root:root 0600 while hidden. The
# daemon's CAP_DAC_OVERRIDE (see keymasqd.service) keeps that node usable
# across the reset, including before the udev ACL re-grant lands.

log = logging.getLogger("keymasqd.force_feedback")
_UINPUT_BEGIN_UPLOAD: Final[str] = "_uinput_begin_upload"
_UINPUT_BEGIN_ERASE: Final[str] = "_uinput_begin_erase"
_WORKER_UPLOAD: Final[Literal["upload"]] = "upload"
_WORKER_ERASE: Final[Literal["erase"]] = "erase"
type _WorkerRequest = tuple[Literal["upload", "erase"], int] | None


class InputEventLike(Protocol):
    type: int
    code: int
    value: int


class ForceFeedbackTarget(Protocol):
    path: str
    ff_effects_count: int

    def upload_effect(self, effect: object) -> int: ...

    def erase_effect(self, ff_id: int) -> None: ...

    def write(self, event_type: int, code: int, value: int) -> None: ...


class ReadableUInput(Protocol):
    fd: int

    def read(self) -> Iterable[InputEventLike]: ...


class ForceFeedbackUInput(ReadableUInput, Protocol):
    def end_upload(self, upload: object) -> None: ...

    def end_erase(self, erase: object) -> None: ...


class _RequestWithRetval(Protocol):
    retval: int


class _EffectLike(Protocol):
    id: int


class _UploadLike(Protocol):
    effect: _EffectLike


class _EraseLike(Protocol):
    effect_id: int


@dataclass(frozen=True)
class EffectMapping:
    physical_id: int


PASSTHROUGH_DIRECT_OUTPUT_EVENT_TYPES: Final[frozenset[int]] = frozenset(
    int(event_type)
    for event_type in (
        getattr(evdev.ecodes, "EV_LED", None),
        getattr(evdev.ecodes, "EV_SND", None),
    )
    if isinstance(event_type, int)
)
PASSTHROUGH_OUTPUT_EVENT_TYPES: Final[frozenset[int]] = frozenset(
    {*PASSTHROUGH_DIRECT_OUTPUT_EVENT_TYPES, int(evdev.ecodes.EV_FF)}
)


def has_passthrough_output_feedback(caps: Mapping[int, Sequence[object]]) -> bool:
    return any(caps.get(event_type) for event_type in PASSTHROUGH_OUTPUT_EVENT_TYPES)


def disable_passthrough_output_feedback(
    caps: dict[int, Sequence[object]],
) -> None:
    for event_type in PASSTHROUGH_OUTPUT_EVENT_TYPES:
        caps.pop(event_type, None)


def passthrough_ff_max_effects(
    caps: Mapping[int, Sequence[object]],
    physical_device: object,
) -> int:
    if not caps.get(int(evdev.ecodes.EV_FF)):
        return 0
    return max(0, int(cast(ForceFeedbackTarget, physical_device).ff_effects_count))


def disable_force_feedback(
    caps: dict[int, Sequence[object]],
) -> None:
    caps.pop(int(evdev.ecodes.EV_FF), None)


def _negative_errno(exc: BaseException, default_errno: int = errno.EIO) -> int:
    if isinstance(exc, OSError) and isinstance(exc.errno, int) and exc.errno > 0:
        return -int(exc.errno)
    return -int(default_errno)


def _uinput_fd(uinput: ReadableUInput) -> int:
    fd = getattr(uinput, "fd", None)
    if isinstance(fd, int):
        return fd
    fileno = cast(Callable[[], int] | None, getattr(uinput, "fileno", None))
    if callable(fileno):
        return int(fileno())
    raise TypeError("uinput object has no fd")


def _uinput_dll(uinput: ForceFeedbackUInput) -> object:
    dll = getattr(uinput, "dll", None)
    if dll is None:
        raise TypeError("uinput object has no evdev dll")
    return dll


def _begin_upload(uinput: ForceFeedbackUInput, request_id: int) -> object:
    # evdev 1.9.x public begin_upload writes the request id to a non-field
    # effect_id attribute, leaving UInputUpload.request_id as 0.
    upload = evdev.ff.UInputUpload()
    upload.request_id = int(request_id)
    begin = cast(Callable[[int, object], int], getattr(_uinput_dll(uinput), _UINPUT_BEGIN_UPLOAD))
    ret = int(begin(_uinput_fd(uinput), ctypes.byref(upload)))
    if ret:
        raise OSError(errno.EIO, f"UI_BEGIN_FF_UPLOAD failed: {ret}")
    return upload


def _end_upload(uinput: ForceFeedbackUInput, upload: object) -> None:
    uinput.end_upload(upload)


def _begin_erase(uinput: ForceFeedbackUInput, request_id: int) -> object:
    # See _begin_upload: evdev 1.9.x public begin_erase also writes the wrong
    # request field before issuing the ioctl.
    erase = evdev.ff.UInputErase()
    erase.request_id = int(request_id)
    begin = cast(Callable[[int, object], int], getattr(_uinput_dll(uinput), _UINPUT_BEGIN_ERASE))
    ret = int(begin(_uinput_fd(uinput), ctypes.byref(erase)))
    if ret:
        raise OSError(errno.EIO, f"UI_BEGIN_FF_ERASE failed: {ret}")
    return erase


def _end_erase(uinput: ForceFeedbackUInput, erase: object) -> None:
    uinput.end_erase(erase)


def _set_retval(request: object, retval: int) -> None:
    try:
        cast(_RequestWithRetval, request).retval = int(retval)
    except (AttributeError, TypeError):
        return


def _effect_id(effect: object) -> int:
    return int(cast(_EffectLike, effect).id)


def _set_effect_id(effect: object, effect_id: int) -> None:
    cast(_EffectLike, effect).id = int(effect_id)


def _erase_effect_id(erase: object) -> int:
    return int(cast(_EraseLike, erase).effect_id)


class PassthroughFeedbackProxy:
    def __init__(
        self,
        uinput: ForceFeedbackUInput,
        physical_device: ForceFeedbackTarget,
        *,
        label: str,
        asyncio_mod: AsyncioRuntimeAdapter = ASYNCIO_RUNTIME,
        logger: logging.Logger = log,
    ) -> None:
        self.uinput = uinput
        self.physical_device = physical_device
        self.label = label
        self.asyncio_mod = asyncio_mod
        self.log = logger
        self._fd: int | None = None
        self._running = False
        self._effects: dict[int, EffectMapping] = {}
        self._effects_lock = threading.RLock()
        self._pending_uploads: set[int] = set()
        self._queued_upload_plays: dict[int, list[int]] = {}
        self._request_queue: asyncio.Queue[_WorkerRequest] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._write_tasks: set[asyncio.Task[None]] = set()

    @property
    def effect_mappings(self) -> Mapping[int, EffectMapping]:
        with self._effects_lock:
            return dict(self._effects)

    def start(self) -> None:
        if self._running:
            return
        fd = _uinput_fd(self.uinput)
        queue: asyncio.Queue[_WorkerRequest] = asyncio.Queue()
        worker_task = self.asyncio_mod.create_task(self._request_worker(queue))
        worker_task.add_done_callback(self._log_worker_result)
        loop = self.asyncio_mod.get_running_loop()
        try:
            loop.add_reader(fd, self._on_readable)
        except Exception:
            worker_task.cancel()
            raise
        self._request_queue = queue
        self._worker_task = worker_task
        self._fd = fd
        self._running = True

    def stop(self) -> None:
        self._stop_reader()

    async def stop_and_wait(self) -> None:
        worker_task = self._stop_reader()
        if worker_task is None or worker_task.done():
            await self._wait_for_write_tasks()
            return
        try:
            await worker_task
        except asyncio.CancelledError:
            await self._wait_for_write_tasks()
            raise
        except Exception:
            self.log.exception("Failed while waiting for force-feedback worker %s", self.label)
        await self._wait_for_write_tasks()

    def _stop_reader(self) -> asyncio.Task[None] | None:
        if not self._running:
            return self._worker_task
        fd = self._fd
        self._running = False
        self._fd = None
        queue = self._request_queue
        worker_task = self._worker_task
        self._request_queue = None
        self._worker_task = None
        if queue is not None and worker_task is not None and not worker_task.done():
            queue.put_nowait(None)
        if fd is None:
            return worker_task
        try:
            self.asyncio_mod.get_running_loop().remove_reader(fd)
        except RuntimeError:
            self.log.debug("No running loop while stopping output-feedback proxy %s", self.label)
        except Exception:
            self.log.exception("Failed to stop output-feedback proxy %s", self.label)
        return worker_task

    def _on_readable(self) -> None:
        try:
            for event in self.uinput.read():
                self.handle_event(event)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                return
            self.log.warning("Output-feedback read failed for %s: %s", self.label, exc)
            self.stop()
        except Exception:
            self.log.exception("Unexpected output-feedback read failure for %s", self.label)
            self.stop()

    def handle_event(self, event: InputEventLike) -> None:
        event_type = int(event.type)
        event_code = int(event.code)
        event_value = int(event.value)
        if event_type == evdev.ecodes.EV_UINPUT:
            if event_code == evdev.ecodes.UI_FF_UPLOAD:
                self._queue_request(_WORKER_UPLOAD, event_value)
            elif event_code == evdev.ecodes.UI_FF_ERASE:
                self._queue_request(_WORKER_ERASE, event_value)
            return

        if event_type in PASSTHROUGH_DIRECT_OUTPUT_EVENT_TYPES:
            self._schedule_physical_event(event_type, event_code, event_value)
            return

        if event_type != evdev.ecodes.EV_FF:
            return

        if event_code in (evdev.ecodes.FF_GAIN, evdev.ecodes.FF_AUTOCENTER):
            self._schedule_physical_ff(event_code, event_value)
            return

        mapping, queued = self._effect_mapping_for_play(event_code, event_value)
        if queued:
            return
        if mapping is None:
            self.log.debug(
                "Dropping force-feedback play for unknown virtual effect %s on %s",
                event_code,
                self.label,
            )
            return
        self._schedule_physical_ff(mapping.physical_id, event_value)

    def _effect_mapping_for_play(
        self,
        virtual_id: int,
        event_value: int,
    ) -> tuple[EffectMapping | None, bool]:
        with self._effects_lock:
            mapping = self._effects.get(virtual_id)
            if mapping is not None:
                return mapping, False
            if virtual_id not in self._pending_uploads:
                return None, False
            self._queued_upload_plays.setdefault(virtual_id, []).append(int(event_value))
            return None, True

    async def _request_worker(self, queue: asyncio.Queue[_WorkerRequest]) -> None:
        while True:
            request = await queue.get()
            if request is None:
                return
            kind, request_id = request
            try:
                if kind == _WORKER_UPLOAD:
                    queued_plays = await self.asyncio_mod.to_thread(
                        self._handle_upload,
                        request_id,
                    )
                    for physical_id, value in queued_plays:
                        self._schedule_physical_ff(physical_id, value)
                elif kind == _WORKER_ERASE:
                    await self.asyncio_mod.to_thread(self._handle_erase, request_id)
            except Exception:
                self.log.exception(
                    "Unexpected force-feedback request failure for %s kind=%s",
                    self.label,
                    kind,
                )

    def _log_worker_result(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            self.log.exception("Force-feedback worker failed for %s", self.label)

    def _queue_request(self, kind: Literal["upload", "erase"], request_id: int) -> None:
        queue = self._request_queue
        if self._running and queue is not None:
            queue.put_nowait((kind, int(request_id)))
            return
        if kind == _WORKER_UPLOAD:
            for physical_id, value in self._handle_upload(request_id):
                self._schedule_physical_ff(physical_id, value)
        else:
            self._handle_erase(request_id)

    def _handle_upload(self, request_id: int) -> list[tuple[int, int]]:
        upload: object | None = None
        retval = 0
        virtual_id: int | None = None
        previous_mapping: EffectMapping | None = None
        physical_id: int | None = None
        published_mapping = False
        queued_physical_plays: list[tuple[int, int]] = []
        try:
            upload = _begin_upload(self.uinput, request_id)
            effect = cast(_UploadLike, upload).effect
            virtual_id = _effect_id(effect)
            with self._effects_lock:
                self._pending_uploads.add(virtual_id)
                previous_mapping = self._effects.get(virtual_id)
            physical_upload_id = (
                previous_mapping.physical_id if previous_mapping is not None else -1
            )
            _set_effect_id(effect, physical_upload_id)
            try:
                physical_id = int(self.physical_device.upload_effect(effect))
            finally:
                _set_effect_id(effect, virtual_id)
            with self._effects_lock:
                self._effects[virtual_id] = EffectMapping(physical_id=physical_id)
                self._pending_uploads.discard(virtual_id)
                queued_values = self._queued_upload_plays.pop(virtual_id, [])
            published_mapping = True
            queued_physical_plays = [
                (physical_id, queued_value) for queued_value in queued_values
            ]
        except Exception as exc:  # noqa: BLE001 - request must be acked on upload failure.
            self._discard_pending_upload(virtual_id)
            retval = _negative_errno(exc)
            self.log.warning(
                "Failed to proxy force-feedback upload for %s: %s",
                self.label,
                exc,
            )
        finally:
            if upload is not None:
                _set_retval(upload, retval)
                try:
                    _end_upload(self.uinput, upload)
                except Exception:
                    self._restore_upload_mapping(
                        virtual_id,
                        previous_mapping=previous_mapping,
                        published_mapping=published_mapping,
                    )
                    self._rollback_uploaded_effect(
                        physical_id,
                        previous_mapping=previous_mapping,
                    )
                    self.log.exception(
                        "Failed to finish force-feedback upload request for %s",
                        self.label,
                    )
                    queued_physical_plays.clear()
        return queued_physical_plays

    def _handle_erase(self, request_id: int) -> None:
        erase: object | None = None
        retval = 0
        virtual_id: int | None = None
        try:
            erase = _begin_erase(self.uinput, request_id)
            virtual_id = _erase_effect_id(erase)
            with self._effects_lock:
                mapping = self._effects.pop(virtual_id, None)
            if mapping is None:
                raise OSError(errno.EINVAL, "unknown force-feedback effect")
            try:
                self.physical_device.erase_effect(mapping.physical_id)
            except Exception:
                with self._effects_lock:
                    self._effects[virtual_id] = mapping
                raise
        except Exception as exc:  # noqa: BLE001 - request must be acked on erase failure.
            retval = _negative_errno(exc, default_errno=errno.EINVAL)
            self.log.warning(
                "Failed to proxy force-feedback erase for %s virtual_id=%s: %s",
                self.label,
                virtual_id,
                exc,
            )
        finally:
            if erase is not None:
                _set_retval(erase, retval)
                try:
                    _end_erase(self.uinput, erase)
                except Exception:
                    self.log.exception(
                        "Failed to finish force-feedback erase request for %s",
                        self.label,
                    )

    def _discard_pending_upload(self, virtual_id: int | None) -> None:
        if virtual_id is None:
            return
        with self._effects_lock:
            self._pending_uploads.discard(virtual_id)
            self._queued_upload_plays.pop(virtual_id, None)

    def _rollback_uploaded_effect(
        self,
        physical_id: int | None,
        *,
        previous_mapping: EffectMapping | None,
    ) -> None:
        if physical_id is None:
            return
        if previous_mapping is not None and physical_id == previous_mapping.physical_id:
            return
        try:
            self.physical_device.erase_effect(physical_id)
        except OSError as exc:
            self.log.warning(
                "Failed to roll back force-feedback upload for %s physical_id=%s: %s",
                self.label,
                physical_id,
                exc,
            )
        except Exception:
            self.log.exception(
                "Unexpected failure rolling back force-feedback upload for %s physical_id=%s",
                self.label,
                physical_id,
            )

    def _restore_upload_mapping(
        self,
        virtual_id: int | None,
        *,
        previous_mapping: EffectMapping | None,
        published_mapping: bool,
    ) -> None:
        if not published_mapping or virtual_id is None:
            return
        with self._effects_lock:
            if previous_mapping is None:
                self._effects.pop(virtual_id, None)
                return
            self._effects[virtual_id] = previous_mapping

    async def _wait_for_write_tasks(self) -> None:
        tasks = set(self._write_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule_physical_ff(self, code: int, value: int) -> None:
        self._schedule_physical_event(evdev.ecodes.EV_FF, code, value)

    def _schedule_physical_event(self, event_type: int, code: int, value: int) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.log.debug(
                "Dropping output event for %s because no event loop is running",
                self.label,
            )
            return
        coro = self._write_physical_event(event_type, code, value)
        try:
            task = self.asyncio_mod.create_task(coro)
        except RuntimeError:
            coro.close()
            self.log.debug(
                "Dropping output event for %s because task scheduling is unavailable",
                self.label,
            )
            return
        self._write_tasks.add(task)
        task.add_done_callback(self._log_write_result)

    def _log_write_result(self, task: asyncio.Task[None]) -> None:
        self._write_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            self.log.exception("Output-feedback write task failed for %s", self.label)

    async def _write_physical_event(self, event_type: int, code: int, value: int) -> None:
        await self.asyncio_mod.to_thread(
            self._write_physical_event_sync,
            event_type,
            code,
            value,
        )

    def _write_physical_event_sync(self, event_type: int, code: int, value: int) -> None:
        try:
            self.physical_device.write(int(event_type), int(code), int(value))
        except OSError as exc:
            self.log.warning(
                "Failed to proxy output event for %s type=%s code=%s value=%s: %s",
                self.label,
                event_type,
                code,
                value,
                exc,
            )
        except Exception:
            self.log.exception(
                "Unexpected failure proxying output event for %s type=%s code=%s value=%s",
                self.label,
                event_type,
                code,
                value,
            )


class OutputFeedbackFanoutProxy:
    """Forward direct output events from one uinput to matching physical devices."""

    def __init__(
        self,
        uinput: ReadableUInput,
        targets_getter: Callable[[int, int], Iterable[ForceFeedbackTarget]],
        *,
        label: str,
        event_types: frozenset[int] = PASSTHROUGH_DIRECT_OUTPUT_EVENT_TYPES,
        asyncio_mod: AsyncioRuntimeAdapter = ASYNCIO_RUNTIME,
        logger: logging.Logger = log,
    ) -> None:
        self.uinput = uinput
        self.targets_getter = targets_getter
        self.label = label
        self.event_types = event_types
        self.asyncio_mod = asyncio_mod
        self.log = logger
        self._fd: int | None = None
        self._running = False
        self._write_tasks: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        if self._running:
            return
        fd = _uinput_fd(self.uinput)
        self.asyncio_mod.get_running_loop().add_reader(fd, self._on_readable)
        self._fd = fd
        self._running = True

    def stop(self) -> None:
        if not self._running:
            return
        fd = self._fd
        self._fd = None
        self._running = False
        if fd is None:
            return
        try:
            self.asyncio_mod.get_running_loop().remove_reader(fd)
        except RuntimeError:
            self.log.debug("No running loop while stopping output-feedback proxy %s", self.label)
        except Exception:
            self.log.exception("Failed to stop output-feedback proxy %s", self.label)

    async def stop_and_wait(self) -> None:
        self.stop()
        tasks = set(self._write_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _on_readable(self) -> None:
        try:
            for event in self.uinput.read():
                self.handle_event(event)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                return
            self.log.warning("Output-feedback read failed for %s: %s", self.label, exc)
            self.stop()
        except Exception:
            self.log.exception("Unexpected output-feedback read failure for %s", self.label)
            self.stop()

    def handle_event(self, event: InputEventLike) -> None:
        event_type = int(event.type)
        if event_type not in self.event_types:
            return
        event_code = int(event.code)
        event_value = int(event.value)
        seen: set[int] = set()
        for target in self.targets_getter(event_type, event_code):
            identity = id(target)
            if identity in seen:
                continue
            seen.add(identity)
            self._schedule_write(target, event_type, event_code, event_value)

    def _schedule_write(
        self,
        target: ForceFeedbackTarget,
        event_type: int,
        code: int,
        value: int,
    ) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.log.debug(
                "Dropping output event for %s because no event loop is running",
                self.label,
            )
            return
        coro = self._write_target(target, event_type, code, value)
        try:
            task = self.asyncio_mod.create_task(coro)
        except RuntimeError:
            coro.close()
            self.log.debug(
                "Dropping output event for %s because task scheduling is unavailable",
                self.label,
            )
            return
        self._write_tasks.add(task)
        task.add_done_callback(self._log_write_result)

    def _log_write_result(self, task: asyncio.Task[None]) -> None:
        self._write_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            self.log.exception("Output-feedback write task failed for %s", self.label)

    async def _write_target(
        self,
        target: ForceFeedbackTarget,
        event_type: int,
        code: int,
        value: int,
    ) -> None:
        await self.asyncio_mod.to_thread(
            self._write_target_sync,
            target,
            event_type,
            code,
            value,
        )

    def _write_target_sync(
        self,
        target: ForceFeedbackTarget,
        event_type: int,
        code: int,
        value: int,
    ) -> None:
        try:
            target.write(event_type, code, value)
        except OSError as exc:
            self.log.warning(
                "Failed to fan out output event for %s type=%s code=%s value=%s: %s",
                self.label,
                event_type,
                code,
                value,
                exc,
            )
        except Exception:
            self.log.exception(
                "Unexpected failure fanning out output event for %s type=%s code=%s value=%s",
                self.label,
                event_type,
                code,
                value,
            )
