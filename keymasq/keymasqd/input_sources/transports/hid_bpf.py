"""Daemon side of a HID-BPF source: request the attachment, then read its maps.

Ring records deliver every state change in order. The state map recovers changes
whose records were lost, and a report count that stops advancing means the
program was detached.
"""

import asyncio
import contextlib
import errno
import mmap
import os
import struct
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path

from keymasq.keymasqd import fd_handoff

from .. import clock
from ..bpf import (
    DRIVER_STATE_OFFSET,
    DRIVER_STATE_SIZE,
    RECORD_SIZE,
    REPORTS_OFFSET,
    RING_SIZE,
    SEQUENCE_OFFSET,
)
from ..types import Binding

HEARTBEAT_S = 0.1
STALL_S = 0.5
HANDOFF_TIMEOUT_S = 2.0
RING_BUSY = 1 << 31
RING_DISCARD = 1 << 30

type Attachment = tuple[int, int, int]
type Attach = Callable[[Binding], Awaitable[Attachment]]


async def request_attachment(binding: Binding) -> Attachment:
    from keymasq.masking.client import request_without_rollback

    token = uuid.uuid4().hex
    async with fd_handoff.handoff_server().expect(token) as pending:
        # An abandoned attachment has nothing to roll back: late descriptors are closed.
        await request_without_rollback(
            "hid-bpf-attach",
            "",
            driver=binding.driver.id,
            device=Path(binding.endpoint.hid_parent).name,
            token=token,
        )
        fds = await pending.claim(HANDOFF_TIMEOUT_S)
    if len(fds) != 3:
        for fd in fds:
            os.close(fd)
        raise OSError(errno.EPROTO, "HID-BPF handoff returned unexpected descriptors")
    return fds[0], fds[1], fds[2]


def _drain(consumer: mmap.mmap, producer: mmap.mmap) -> list[tuple[int, bytes]]:
    records: list[tuple[int, bytes]] = []
    head = struct.unpack_from("<Q", consumer, 0)[0]
    tail = struct.unpack_from("<Q", producer, 0)[0]
    while head < tail:
        offset = mmap.PAGESIZE + (head & (RING_SIZE - 1))
        length = struct.unpack_from("<I", producer, offset)[0]
        if length & RING_BUSY:
            break
        size = length & ~(RING_BUSY | RING_DISCARD)
        if not length & RING_DISCARD and size >= RECORD_SIZE:
            sequence = struct.unpack_from("<Q", producer, offset + 8)[0]
            records.append((sequence, bytes(producer[offset + 16 : offset + 8 + RECORD_SIZE])))
        head += (size + 8 + 7) & ~7
    struct.pack_into("<Q", consumer, 0, head)
    return records


async def reports(
    binding: Binding, *, attach: Attach = request_attachment
) -> AsyncGenerator[bytes]:
    fds = await attach(binding)
    _link_fd, ring_fd, state_fd = fds
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()
    maps: list[mmap.mmap] = []
    try:
        page = mmap.PAGESIZE
        consumer = mmap.mmap(ring_fd, page, prot=mmap.PROT_READ | mmap.PROT_WRITE)
        maps.append(consumer)
        producer = mmap.mmap(ring_fd, page + 2 * RING_SIZE, prot=mmap.PROT_READ, offset=page)
        maps.append(producer)
        state = mmap.mmap(state_fd, page, prot=mmap.PROT_READ)
        maps.append(state)
        loop.add_reader(ring_fd, ready.set)
        try:
            seen = struct.unpack_from("<Q", state, REPORTS_OFFSET)[0]
            progress_ns = time.monotonic_ns()
            suspended_at_ns = clock.suspended_ns()
            delivered = 0
            while True:
                due_ns = time.monotonic_ns() + int(HEARTBEAT_S * 1_000_000_000)
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(HEARTBEAT_S):
                        await ready.wait()
                ready.clear()
                count = struct.unpack_from("<Q", state, REPORTS_OFFSET)[0]
                now_ns = time.monotonic_ns()
                if count != seen or clock.unobserved(due_ns, suspended_at_ns):
                    seen, progress_ns = count, now_ns
                    suspended_at_ns = clock.suspended_ns()
                elif now_ns - progress_ns > STALL_S * 1_000_000_000:
                    raise OSError(errno.ENODEV, "HID-BPF source stopped receiving reports")
                # The program stores the state before submitting its record, so read it first.
                sequence = struct.unpack_from("<Q", state, SEQUENCE_OFFSET)[0]
                current = bytes(
                    state[DRIVER_STATE_OFFSET : DRIVER_STATE_OFFSET + DRIVER_STATE_SIZE]
                )
                changes = [item for item in _drain(consumer, producer) if item[0] > delivered]
                # The state is ahead for a record not yet submitted or lost to a full ring.
                if sequence > max((item[0] for item in changes), default=delivered):
                    changes.append((sequence, current))
                if not changes:
                    yield current
                for sequence, change in changes:
                    delivered = sequence
                    yield change
        finally:
            loop.remove_reader(ring_fd)
    finally:
        for mapping in maps:
            mapping.close()
        for fd in fds:
            os.close(fd)
