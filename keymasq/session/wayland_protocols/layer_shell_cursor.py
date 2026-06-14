from __future__ import annotations

import asyncio
import contextlib
import logging
import mmap
import os
import struct
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from keymasq.session.cursor_position import trigger_cursor_position_sample
from keymasq.session.wayland_protocols import client_transport as _transport

if TYPE_CHECKING:
    from keymasq.session.client import KeymasqdClient

log = logging.getLogger("keymasq-session.wayland.layer_shell_cursor")

WL_COMPOSITOR_INTERFACE = "wl_compositor"
WL_OUTPUT_INTERFACE = "wl_output"
WL_SEAT_INTERFACE = "wl_seat"
WL_POINTER_INTERFACE = "wl_pointer"
WL_SHM_INTERFACE = "wl_shm"
WL_SHM_POOL_INTERFACE = "wl_shm_pool"
WL_BUFFER_INTERFACE = "wl_buffer"
WL_SURFACE_INTERFACE = "wl_surface"
XDG_OUTPUT_MANAGER_INTERFACE = "zxdg_output_manager_v1"
XDG_OUTPUT_INTERFACE = "zxdg_output_v1"
WLR_LAYER_SHELL_INTERFACE = "zwlr_layer_shell_v1"
WLR_LAYER_SURFACE_INTERFACE = "zwlr_layer_surface_v1"

WL_SEAT_CAPABILITY_POINTER = 1
WL_SHM_FORMAT_ARGB8888 = 0
WLR_LAYER_OVERLAY = 3
WLR_LAYER_ANCHOR_ALL = 1 | 2 | 4 | 8
WLR_LAYER_KEYBOARD_INTERACTIVITY_NONE = 0

DEFAULT_TRACKING_HINT_MS = 250
TRACKING_GRACE_MS = 40
INITIAL_SAMPLE_TIMEOUT_S = 0.08
MAP_SURFACE_TIMEOUT_S = 0.35
SAMPLE_MAX_AGE_S = 0.12
NEXT_SAMPLE_TIMEOUT_S = 0.005
SURFACE_DESTROY_SYNC_TIMEOUT_S = 0.15


def _pack_int(value: int) -> bytes:
    return struct.pack("<i", int(value))


def _fixed_to_float(value: int) -> float:
    return float(value) / 256.0


def _decode_fixed(payload: bytes, offset: int) -> tuple[float, int]:
    (raw,) = struct.unpack_from("<i", payload, offset)
    return _fixed_to_float(raw), offset + 4


@dataclass(slots=True)
class OutputGeometry:
    x: int
    y: int
    width: int
    height: int
    generation: int


@dataclass(slots=True)
class _OutputState:
    global_name: int
    output_id: int
    output_version: int
    xdg_output_id: int | None = None
    xdg_output_version: int = 0
    logical_x: int | None = None
    logical_y: int | None = None
    logical_width: int | None = None
    logical_height: int | None = None
    name: str = ""
    generation: int = 0
    closed: bool = False

    def geometry(self, global_generation: int) -> OutputGeometry | None:
        if self.closed:
            return None
        if (
            self.logical_x is None
            or self.logical_y is None
            or self.logical_width is None
            or self.logical_height is None
        ):
            return None
        if self.logical_width <= 0 or self.logical_height <= 0:
            return None
        return OutputGeometry(
            x=int(self.logical_x),
            y=int(self.logical_y),
            width=int(self.logical_width),
            height=int(self.logical_height),
            generation=global_generation,
        )


@dataclass(slots=True)
class _SurfaceState:
    output_id: int
    geometry: OutputGeometry
    surface_id: int
    layer_surface_id: int
    configured_width: int = 0
    configured_height: int = 0
    buffer_id: int | None = None
    mapping: mmap.mmap | None = None
    configured_event: asyncio.Event = field(default_factory=asyncio.Event)
    mapped_event: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False

    @property
    def width(self) -> int:
        return self.configured_width or self.geometry.width

    @property
    def height(self) -> int:
        return self.configured_height or self.geometry.height

    def close_mapping(self) -> None:
        if self.mapping is not None:
            with contextlib.suppress(BufferError, OSError, ValueError):
                self.mapping.close()
            self.mapping = None


@dataclass(frozen=True, slots=True)
class CursorSample:
    x: int
    y: int
    generation: int
    sequence: int
    received_at: float


class LayerShellCursorTracker(_transport.WaylandClientTransport):
    def __init__(
        self,
        daemon_client: KeymasqdClient | None = None,
        socket_path: str | None = None,
    ) -> None:
        super().__init__(socket_path)
        self._daemon_client = daemon_client
        self._compositor_id: int | None = None
        self._shm_id: int | None = None
        self._layer_shell_id: int | None = None
        self._xdg_output_manager_id: int | None = None
        self._xdg_output_manager_version = 0
        self._seat_ids: set[int] = set()
        self._pointer_id: int | None = None
        self._outputs_by_global: dict[int, _OutputState] = {}
        self._outputs_by_object: dict[int, _OutputState] = {}
        self._outputs_by_xdg_object: dict[int, _OutputState] = {}
        self._surfaces_by_surface: dict[int, _SurfaceState] = {}
        self._surfaces_by_layer_surface: dict[int, _SurfaceState] = {}
        self._surfaces_by_output: dict[int, _SurfaceState] = {}
        self._active_surface_id: int | None = None
        self._geometry_generation = 0
        self._surface_generation = 0
        self._sample_sequence = 0
        self._last_returned_sample_sequence = 0
        self._sample: CursorSample | None = None
        self._sample_event = asyncio.Event()
        self._mapping_lock = asyncio.Lock()
        self._mapped_until = 0.0
        self._unmap_task: asyncio.Task[None] | None = None

    @property
    def supports_cursor_tracking(self) -> bool:
        return (
            self._socket is not None
            and self._compositor_id is not None
            and self._shm_id is not None
            and self._layer_shell_id is not None
            and self._xdg_output_manager_id is not None
            and self._pointer_id is not None
            and bool(self._ready_output_geometries())
        )

    def _check_required_globals(self) -> None:
        missing: list[str] = []
        if self._compositor_id is None:
            missing.append(WL_COMPOSITOR_INTERFACE)
        if self._shm_id is None:
            missing.append(WL_SHM_INTERFACE)
        if self._layer_shell_id is None:
            missing.append(WLR_LAYER_SHELL_INTERFACE)
        if self._xdg_output_manager_id is None:
            missing.append(XDG_OUTPUT_MANAGER_INTERFACE)
        if not self._outputs_by_object:
            missing.append(WL_OUTPUT_INTERFACE)
        if not self._seat_ids:
            missing.append(WL_SEAT_INTERFACE)
        if missing:
            raise RuntimeError(
                "Wayland layer-shell cursor tracking is unavailable; missing "
                + ", ".join(missing)
            )

    def _after_start_sync(self) -> None:
        if self._pointer_id is None:
            raise RuntimeError("Wayland layer-shell cursor tracking requires a pointer seat")
        if not self._ready_output_geometries():
            raise RuntimeError("Wayland layer-shell cursor tracking requires xdg-output geometry")

    async def stop(self) -> None:
        self._running = False
        task = self._unmap_task
        self._unmap_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if self._socket is not None:
            await self.stop_cursor_position_tracking(nudge_after_stop=False)
            await self._release_pointer()
            for output in list(self._outputs_by_object.values()):
                await self._destroy_output(output)
            if self._xdg_output_manager_id is not None:
                with contextlib.suppress(OSError, RuntimeError, _transport.WaylandDisplayError):
                    await self._send_request(self._xdg_output_manager_id, 0, b"")
                self._objects.pop(self._xdg_output_manager_id, None)
                self._xdg_output_manager_id = None
            if self._layer_shell_id is not None and self._layer_shell_id in self._objects:
                with contextlib.suppress(OSError, RuntimeError, _transport.WaylandDisplayError):
                    await self._send_request(self._layer_shell_id, 1, b"")
                self._objects.pop(self._layer_shell_id, None)
                self._layer_shell_id = None
            self._close_socket()

    async def prepare_cursor_position_tracking(self, duration_ms: int) -> None:
        duration_ms = max(1, int(duration_ms))
        await self._ensure_mapped(duration_ms)
        if not self._sample_is_fresh():
            await self._trigger_sample()
            await self._wait_for_fresh_sample(INITIAL_SAMPLE_TIMEOUT_S)

    async def stop_cursor_position_tracking(self, *, nudge_after_stop: bool = True) -> None:
        surface_count = len(self._surfaces_by_surface)
        if surface_count:
            log.debug(
                "Stopping layer-shell cursor tracking: surfaces=%s active_surface_id=%s",
                surface_count,
                self._active_surface_id,
            )
        self._mapped_until = 0.0
        task = self._unmap_task
        self._unmap_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        destroyed_surfaces = False
        async with self._mapping_lock:
            destroyed_surfaces = bool(self._surfaces_by_surface)
            await self._destroy_all_surfaces()
            self._active_surface_id = None
            self._sample = None
            self._last_returned_sample_sequence = 0
            self._sample_event = asyncio.Event()

        if destroyed_surfaces:
            try:
                await self._roundtrip(timeout=SURFACE_DESTROY_SYNC_TIMEOUT_S)
            except (OSError, RuntimeError, TimeoutError, _transport.WaylandDisplayError):
                log.debug("Failed to sync layer-shell cursor surface teardown", exc_info=True)
            if nudge_after_stop:
                log.debug("Nudging pointer after layer-shell cursor surfaces were destroyed")
                await self._trigger_sample()

    async def get_cursor_position(self) -> tuple[int, int] | None:
        if not self.supports_cursor_tracking:
            return None
        if not self._mapping_active():
            await self.prepare_cursor_position_tracking(DEFAULT_TRACKING_HINT_MS)

        previous_sequence = self._sample.sequence if self._sample is not None else 0
        needs_new_sample = (
            self._sample_is_fresh()
            and self._sample is not None
            and self._sample.sequence <= self._last_returned_sample_sequence
        )
        if needs_new_sample:
            await self._wait_for_newer_sample(previous_sequence, timeout_s=NEXT_SAMPLE_TIMEOUT_S)

        if (
            not self._sample_is_fresh()
            or self._sample is None
            or (needs_new_sample and self._sample.sequence <= previous_sequence)
        ):
            await self._trigger_sample()
            if previous_sequence > 0:
                await self._wait_for_newer_sample(
                    previous_sequence,
                    timeout_s=INITIAL_SAMPLE_TIMEOUT_S,
                )
            else:
                await self._wait_for_fresh_sample(INITIAL_SAMPLE_TIMEOUT_S)
        if not self._sample_is_fresh():
            return None
        sample = self._sample
        if sample is None:
            return None
        if (
            needs_new_sample
            and self._daemon_client is not None
            and sample.sequence <= previous_sequence
        ):
            return None
        self._last_returned_sample_sequence = sample.sequence
        return sample.x, sample.y

    async def _ensure_mapped(self, duration_ms: int) -> None:
        if not self.supports_cursor_tracking:
            return

        deadline = time.monotonic() + (max(1, int(duration_ms)) + TRACKING_GRACE_MS) / 1000.0
        self._mapped_until = max(self._mapped_until, deadline)
        self._schedule_unmap()

        async with self._mapping_lock:
            geometries = self._ready_output_geometries()
            if not geometries:
                return
            needs_rebuild = self._surface_generation != self._geometry_generation
            if needs_rebuild:
                await self._destroy_all_surfaces()
            for output_id, geometry in geometries.items():
                surface = self._surfaces_by_output.get(output_id)
                if surface is None or surface.geometry != geometry or surface.closed:
                    if surface is not None:
                        await self._destroy_surface(surface)
                    await self._create_layer_surface(output_id, geometry)
            self._surface_generation = self._geometry_generation

        await self._wait_for_surfaces_mapped()

    def _mapping_active(self) -> bool:
        return self._mapped_until > time.monotonic() and bool(self._surfaces_by_surface)

    def _schedule_unmap(self) -> None:
        task = self._unmap_task
        if task is not None and not task.done():
            task.cancel()
        self._unmap_task = asyncio.create_task(
            self._unmap_after_deadline(),
            name="keymasq-session:wayland-layer-cursor-unmap",
        )

    async def _unmap_after_deadline(self) -> None:
        try:
            while True:
                delay = self._mapped_until - time.monotonic()
                if delay <= 0.0:
                    break
                await asyncio.sleep(delay)
            async with self._mapping_lock:
                await self._destroy_all_surfaces()
                self._active_surface_id = None
                self._sample = None
                self._sample_event = asyncio.Event()
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, _transport.WaylandDisplayError):
            log.debug("Failed to unmap layer-shell cursor surfaces", exc_info=True)
        finally:
            if self._unmap_task is asyncio.current_task():
                self._unmap_task = None

    async def _wait_for_surfaces_mapped(self) -> None:
        surfaces = list(self._surfaces_by_surface.values())
        if not surfaces:
            return
        waits = [
            asyncio.wait_for(surface.mapped_event.wait(), timeout=MAP_SURFACE_TIMEOUT_S)
            for surface in surfaces
            if not surface.mapped_event.is_set()
        ]
        if not waits:
            return
        results = await asyncio.gather(*waits, return_exceptions=True)
        for result in results:
            if isinstance(result, TimeoutError):
                log.debug("Timed out waiting for layer-shell cursor surface to map")

    async def _wait_for_fresh_sample(self, timeout_s: float) -> None:
        if self._sample_is_fresh():
            return
        try:
            await asyncio.wait_for(self._sample_event.wait(), timeout=timeout_s)
        except TimeoutError:
            return

    async def _wait_for_newer_sample(self, previous_sequence: int, timeout_s: float) -> None:
        if previous_sequence <= 0:
            return
        if self._sample is not None and self._sample.sequence > previous_sequence:
            return
        try:
            await asyncio.wait_for(self._sample_event.wait(), timeout=timeout_s)
        except TimeoutError:
            return

    def _sample_is_fresh(self) -> bool:
        sample = self._sample
        if sample is None:
            return False
        if sample.generation != self._geometry_generation:
            return False
        return (time.monotonic() - sample.received_at) <= SAMPLE_MAX_AGE_S

    async def _trigger_sample(self) -> None:
        if self._daemon_client is None:
            return
        await trigger_cursor_position_sample(self._daemon_client)

    def _ready_output_geometries(self) -> dict[int, OutputGeometry]:
        geometries: dict[int, OutputGeometry] = {}
        for output_id, output in self._outputs_by_object.items():
            geometry = output.geometry(self._geometry_generation)
            if geometry is not None:
                geometries[output_id] = geometry
        return geometries

    async def _create_layer_surface(self, output_id: int, geometry: OutputGeometry) -> None:
        if self._compositor_id is None or self._layer_shell_id is None:
            return

        surface_id = self._allocate_object_id(WL_SURFACE_INTERFACE)
        layer_surface_id = self._allocate_object_id(WLR_LAYER_SURFACE_INTERFACE)
        surface = _SurfaceState(
            output_id=output_id,
            geometry=geometry,
            surface_id=surface_id,
            layer_surface_id=layer_surface_id,
        )
        log.debug(
            "Creating layer-shell cursor surface: output_id=%s geometry=%s,%s %sx%s "
            "surface_id=%s layer_surface_id=%s",
            output_id,
            geometry.x,
            geometry.y,
            geometry.width,
            geometry.height,
            surface_id,
            layer_surface_id,
        )
        self._surfaces_by_surface[surface_id] = surface
        self._surfaces_by_layer_surface[layer_surface_id] = surface
        self._surfaces_by_output[output_id] = surface

        await self._send_request(self._compositor_id, 0, _transport.pack_uint(surface_id))
        await self._send_request(
            self._layer_shell_id,
            0,
            _transport.pack_uint(layer_surface_id)
            + _transport.pack_uint(surface_id)
            + _transport.pack_uint(output_id)
            + _transport.pack_uint(WLR_LAYER_OVERLAY)
            + _transport.encode_string("keymasq-cursor-position"),
        )
        await self._send_request(layer_surface_id, 0, _transport.pack_uint(0) * 2)
        await self._send_request(layer_surface_id, 1, _transport.pack_uint(WLR_LAYER_ANCHOR_ALL))
        await self._send_request(layer_surface_id, 2, _pack_int(-1))
        await self._send_request(
            layer_surface_id,
            4,
            _transport.pack_uint(WLR_LAYER_KEYBOARD_INTERACTIVITY_NONE),
        )
        await self._send_request(surface_id, 6, b"")

    async def _map_configured_surface(self, surface: _SurfaceState, serial: int) -> None:
        if surface.closed:
            return
        await self._send_request(surface.layer_surface_id, 6, _transport.pack_uint(serial))
        width = max(1, int(surface.width))
        height = max(1, int(surface.height))
        buffer_id, mapping = await self._create_transparent_buffer(width, height)
        log.debug(
            "Mapping layer-shell cursor surface: output_id=%s surface_id=%s "
            "layer_surface_id=%s buffer_id=%s size=%sx%s",
            surface.output_id,
            surface.surface_id,
            surface.layer_surface_id,
            buffer_id,
            width,
            height,
        )
        surface.buffer_id = buffer_id
        surface.mapping = mapping
        await self._send_request(
            surface.surface_id,
            1,
            _transport.pack_uint(buffer_id) + _pack_int(0) + _pack_int(0),
        )
        await self._send_request(
            surface.surface_id,
            2,
            _pack_int(0) + _pack_int(0) + _pack_int(width) + _pack_int(height),
        )
        await self._send_request(surface.surface_id, 6, b"")
        surface.mapped_event.set()

    async def _create_transparent_buffer(self, width: int, height: int) -> tuple[int, mmap.mmap]:
        if self._shm_id is None:
            raise RuntimeError("wl_shm is unavailable")
        stride = max(1, int(width)) * 4
        size = stride * max(1, int(height))
        fd = os.memfd_create("keymasq-layer-cursor", os.MFD_CLOEXEC)
        try:
            os.ftruncate(fd, size)
            mapping = mmap.mmap(fd, size)
            pool_id = self._allocate_object_id(WL_SHM_POOL_INTERFACE)
            await self._send_request_with_fds(
                self._shm_id,
                0,
                _transport.pack_uint(pool_id) + _pack_int(size),
                [fd],
            )
        finally:
            os.close(fd)

        buffer_id = self._allocate_object_id(WL_BUFFER_INTERFACE)
        await self._send_request(
            pool_id,
            0,
            _transport.pack_uint(buffer_id)
            + _pack_int(0)
            + _pack_int(width)
            + _pack_int(height)
            + _pack_int(stride)
            + _transport.pack_uint(WL_SHM_FORMAT_ARGB8888),
        )
        await self._send_request(pool_id, 1, b"")
        self._objects.pop(pool_id, None)
        return buffer_id, mapping

    async def _destroy_all_surfaces(self) -> None:
        for surface in list(self._surfaces_by_surface.values()):
            await self._destroy_surface(surface)
        self._surfaces_by_surface.clear()
        self._surfaces_by_layer_surface.clear()
        self._surfaces_by_output.clear()

    async def _destroy_surface(self, surface: _SurfaceState) -> None:
        surface.closed = True
        log.debug(
            "Destroying layer-shell cursor surface: output_id=%s surface_id=%s "
            "layer_surface_id=%s buffer_id=%s",
            surface.output_id,
            surface.surface_id,
            surface.layer_surface_id,
            surface.buffer_id,
        )
        if self._socket is not None:
            if surface.surface_id in self._objects:
                with contextlib.suppress(OSError, RuntimeError, _transport.WaylandDisplayError):
                    await self._send_request(
                        surface.surface_id,
                        1,
                        _transport.pack_uint(0) + _pack_int(0) + _pack_int(0),
                    )
                    await self._send_request(surface.surface_id, 6, b"")
            if surface.layer_surface_id in self._objects:
                with contextlib.suppress(OSError, RuntimeError, _transport.WaylandDisplayError):
                    await self._send_request(surface.layer_surface_id, 7, b"")
            if surface.surface_id in self._objects:
                with contextlib.suppress(OSError, RuntimeError, _transport.WaylandDisplayError):
                    await self._send_request(surface.surface_id, 0, b"")
            if surface.buffer_id is not None and surface.buffer_id in self._objects:
                with contextlib.suppress(OSError, RuntimeError, _transport.WaylandDisplayError):
                    await self._send_request(surface.buffer_id, 0, b"")
        self._objects.pop(surface.layer_surface_id, None)
        self._objects.pop(surface.surface_id, None)
        if surface.buffer_id is not None:
            self._objects.pop(surface.buffer_id, None)
        self._surfaces_by_surface.pop(surface.surface_id, None)
        self._surfaces_by_layer_surface.pop(surface.layer_surface_id, None)
        self._surfaces_by_output.pop(surface.output_id, None)
        if self._active_surface_id == surface.surface_id:
            self._active_surface_id = None
        surface.close_mapping()

    async def _destroy_output(self, output: _OutputState) -> None:
        output.closed = True
        surface = self._surfaces_by_output.get(output.output_id)
        if surface is not None:
            await self._destroy_surface(surface)
        if output.xdg_output_id is not None and output.xdg_output_id in self._objects:
            with contextlib.suppress(OSError, RuntimeError, _transport.WaylandDisplayError):
                await self._send_request(output.xdg_output_id, 0, b"")
            self._objects.pop(output.xdg_output_id, None)
        if output.output_id in self._objects:
            with contextlib.suppress(OSError, RuntimeError, _transport.WaylandDisplayError):
                await self._send_request(output.output_id, 0, b"")
            self._objects.pop(output.output_id, None)
        self._outputs_by_global.pop(output.global_name, None)
        self._outputs_by_object.pop(output.output_id, None)
        if output.xdg_output_id is not None:
            self._outputs_by_xdg_object.pop(output.xdg_output_id, None)
        self._invalidate_geometry()

    async def _release_pointer(self) -> None:
        pointer_id = self._pointer_id
        if pointer_id is None:
            return
        self._pointer_id = None
        if pointer_id in self._objects and self._socket is not None:
            with contextlib.suppress(OSError, RuntimeError, _transport.WaylandDisplayError):
                await self._send_request(pointer_id, 1, b"")
        self._objects.pop(pointer_id, None)

    async def _dispatch_protocol_event(
        self,
        interface: str,
        object_id: int,
        opcode: int,
        payload: bytes,
    ) -> None:
        if interface == WL_OUTPUT_INTERFACE:
            self._handle_output_event(object_id, opcode, payload)
            return
        if interface == XDG_OUTPUT_INTERFACE:
            self._handle_xdg_output_event(object_id, opcode, payload)
            return
        if interface == WL_SEAT_INTERFACE:
            await self._handle_seat_event(object_id, opcode, payload)
            return
        if interface == WL_POINTER_INTERFACE:
            self._handle_pointer_event(opcode, payload)
            return
        if interface == WLR_LAYER_SURFACE_INTERFACE:
            await self._handle_layer_surface_event(object_id, opcode, payload)
            return

    async def _handle_registry_global(
        self,
        registry_id: int,
        global_name: int,
        interface_name: str,
        version: int,
    ) -> None:
        if interface_name == WL_COMPOSITOR_INTERFACE and self._compositor_id is None:
            self._compositor_id = await self._bind_global(
                registry_id,
                global_name,
                interface_name,
                max_version=min(version, 4),
            )
            return
        if interface_name == WL_SHM_INTERFACE and self._shm_id is None:
            self._shm_id = await self._bind_global(
                registry_id,
                global_name,
                interface_name,
                max_version=min(version, 1),
            )
            return
        if interface_name == WLR_LAYER_SHELL_INTERFACE and self._layer_shell_id is None:
            self._layer_shell_id = await self._bind_global(
                registry_id,
                global_name,
                interface_name,
                max_version=min(version, 3),
            )
            return
        if interface_name == XDG_OUTPUT_MANAGER_INTERFACE and self._xdg_output_manager_id is None:
            self._xdg_output_manager_version = max(1, min(version, 3))
            self._xdg_output_manager_id = await self._bind_global(
                registry_id,
                global_name,
                interface_name,
                max_version=self._xdg_output_manager_version,
            )
            for output in list(self._outputs_by_object.values()):
                await self._bind_xdg_output(output)
            return
        if interface_name == WL_OUTPUT_INTERFACE:
            output_id = await self._bind_global(
                registry_id,
                global_name,
                interface_name,
                max_version=min(version, 3),
            )
            output = _OutputState(
                global_name=global_name,
                output_id=output_id,
                output_version=max(1, min(version, 3)),
            )
            self._outputs_by_global[global_name] = output
            self._outputs_by_object[output_id] = output
            await self._bind_xdg_output(output)
            return
        if interface_name == WL_SEAT_INTERFACE:
            seat_id = await self._bind_global(
                registry_id,
                global_name,
                interface_name,
                max_version=min(version, 5),
            )
            self._seat_ids.add(seat_id)

    async def _handle_registry_global_remove(self, global_name: int) -> None:
        output = self._outputs_by_global.get(global_name)
        if output is not None:
            await self._destroy_output(output)

    async def _bind_global(
        self,
        registry_id: int,
        global_name: int,
        interface_name: str,
        *,
        max_version: int,
    ) -> int:
        object_id = self._allocate_object_id(interface_name)
        await self._bind_registry_global(
            registry_id,
            global_name,
            interface_name,
            max(1, int(max_version)),
            object_id,
        )
        return object_id

    async def _bind_xdg_output(self, output: _OutputState) -> None:
        if self._xdg_output_manager_id is None or output.xdg_output_id is not None:
            return
        xdg_output_id = self._allocate_object_id(XDG_OUTPUT_INTERFACE)
        output.xdg_output_id = xdg_output_id
        output.xdg_output_version = self._xdg_output_manager_version
        self._outputs_by_xdg_object[xdg_output_id] = output
        await self._send_request(
            self._xdg_output_manager_id,
            1,
            _transport.pack_uint(xdg_output_id) + _transport.pack_uint(output.output_id),
        )

    def _handle_output_event(self, object_id: int, opcode: int, payload: bytes) -> None:
        output = self._outputs_by_object.get(object_id)
        if output is None:
            return
        if opcode == 2:
            self._publish_output_geometry(output)
            return
        if opcode == 4:
            name, _offset = _transport.decode_string(payload, 0)
            output.name = name

    def _handle_xdg_output_event(self, object_id: int, opcode: int, payload: bytes) -> None:
        output = self._outputs_by_xdg_object.get(object_id)
        if output is None:
            return
        if opcode == 0:
            x, y = struct.unpack_from("<ii", payload, 0)
            output.logical_x = int(x)
            output.logical_y = int(y)
            return
        if opcode == 1:
            width, height = struct.unpack_from("<ii", payload, 0)
            output.logical_width = int(width)
            output.logical_height = int(height)
            return
        if opcode == 2 and output.xdg_output_version < 3:
            self._publish_output_geometry(output)
            return
        if opcode == 3:
            name, _offset = _transport.decode_string(payload, 0)
            output.name = name

    def _publish_output_geometry(self, output: _OutputState) -> None:
        previous = output.generation
        output.generation += 1
        geometry = output.geometry(self._geometry_generation)
        if geometry is None:
            return
        if output.generation != previous:
            self._invalidate_geometry()

    def _invalidate_geometry(self) -> None:
        self._geometry_generation += 1
        self._sample = None
        self._sample_event = asyncio.Event()

    async def _handle_seat_event(self, object_id: int, opcode: int, payload: bytes) -> None:
        if object_id not in self._seat_ids or opcode != 0:
            return
        (capabilities,) = struct.unpack_from("<I", payload, 0)
        has_pointer = bool(capabilities & WL_SEAT_CAPABILITY_POINTER)
        if has_pointer and self._pointer_id is None:
            pointer_id = self._allocate_object_id(WL_POINTER_INTERFACE)
            await self._send_request(object_id, 0, _transport.pack_uint(pointer_id))
            self._pointer_id = pointer_id
            return
        if not has_pointer and self._pointer_id is not None:
            await self._release_pointer()

    async def _handle_layer_surface_event(
        self,
        object_id: int,
        opcode: int,
        payload: bytes,
    ) -> None:
        surface = self._surfaces_by_layer_surface.get(object_id)
        if surface is None:
            return
        if opcode == 0:
            serial, width, height = struct.unpack_from("<III", payload, 0)
            surface.configured_width = int(width)
            surface.configured_height = int(height)
            surface.configured_event.set()
            await self._map_configured_surface(surface, int(serial))
            return
        if opcode == 1:
            await self._destroy_surface(surface)

    def _handle_pointer_event(self, opcode: int, payload: bytes) -> None:
        if opcode == 0:
            _serial, surface_id = struct.unpack_from("<II", payload, 0)
            surface_x, offset = _decode_fixed(payload, 8)
            surface_y, _offset = _decode_fixed(payload, offset)
            if surface_id in self._surfaces_by_surface:
                self._active_surface_id = surface_id
                self._update_cursor_sample(surface_id, surface_x, surface_y)
            return
        if opcode == 1:
            _serial, surface_id = struct.unpack_from("<II", payload, 0)
            if self._active_surface_id == surface_id:
                self._active_surface_id = None
            return
        if opcode == 2 and self._active_surface_id is not None:
            surface_x, offset = _decode_fixed(payload, 4)
            surface_y, _offset = _decode_fixed(payload, offset)
            self._update_cursor_sample(self._active_surface_id, surface_x, surface_y)

    def _update_cursor_sample(self, surface_id: int, surface_x: float, surface_y: float) -> None:
        surface = self._surfaces_by_surface.get(surface_id)
        if surface is None or surface.geometry.generation != self._geometry_generation:
            return
        x = int(round(surface.geometry.x + surface_x))
        y = int(round(surface.geometry.y + surface_y))
        self._sample_sequence += 1
        self._sample = CursorSample(
            x=x,
            y=y,
            generation=self._geometry_generation,
            sequence=self._sample_sequence,
            received_at=time.monotonic(),
        )
        self._sample_event.set()
        self._sample_event = asyncio.Event()

    def _on_object_deleted(self, object_id: int) -> None:
        self._objects.pop(object_id, None)
