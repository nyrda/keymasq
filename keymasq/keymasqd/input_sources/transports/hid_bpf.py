"""Daemon side of a HID-BPF source: request the attachment, then read its maps.

The ring buffer only wakes the reader; the state map is the source of truth, and
a report count that stops advancing means the program was detached.
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

from ..bpf import REPORTS_OFFSET, RING_SIZE, STATE_SIZE
from ..types import Binding

HEARTBEAT_S = 0.1
STALL_S = 0.5
HANDOFF_TIMEOUT_S = 2.0
RING_BUSY = 1 << 31
RING_DISCARD = 1 << 30

type Attachment = tuple[int, int, int]
type Attach = Callable[[Binding], Awaitable[Attachment]]


async def request_attachment(binding: Binding) -> Attachment:
    from keymasq.masking.client import request

    token = uuid.uuid4().hex
    async with fd_handoff.handoff_server().expect(token) as pending:
        await request(
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


def _drain(consumer: mmap.mmap, producer: mmap.mmap) -> None:
    head = struct.unpack_from("<Q", consumer, 0)[0]
    tail = struct.unpack_from("<Q", producer, 0)[0]
    while head < tail:
        length = struct.unpack_from("<I", producer, mmap.PAGESIZE + (head & (RING_SIZE - 1)))[0]
        if length & RING_BUSY:
            break
        head += ((length & ~(RING_BUSY | RING_DISCARD)) + 8 + 7) & ~7
    struct.pack_into("<Q", consumer, 0, head)


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
            progress_at = time.monotonic()
            while True:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(HEARTBEAT_S):
                        await ready.wait()
                ready.clear()
                _drain(consumer, producer)
                count = struct.unpack_from("<Q", state, REPORTS_OFFSET)[0]
                now = time.monotonic()
                if count != seen:
                    seen, progress_at = count, now
                elif now - progress_at > STALL_S:
                    raise OSError(errno.ENODEV, "HID-BPF source stopped receiving reports")
                yield bytes(state[:STATE_SIZE])
        finally:
            loop.remove_reader(ring_fd)
    finally:
        for mapping in maps:
            mapping.close()
        for fd in fds:
            os.close(fd)
