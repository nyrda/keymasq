"""Load bundled HID-BPF programs with raw bpf(2) calls, run only by the root helper.

Programs are assembled from driver code, never read from files or requests.
"""

import ctypes
import errno
import os
import platform
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .types import InputDriver

SYS_BPF = {"x86_64": 321, "aarch64": 280, "riscv64": 280}.get(platform.machine())
BPF_MAP_CREATE = 0
BPF_MAP_UPDATE_ELEM = 2
BPF_PROG_LOAD = 5
BPF_BTF_LOAD = 18
BPF_LINK_CREATE = 28
BPF_MAP_TYPE_ARRAY = 2
BPF_MAP_TYPE_STRUCT_OPS = 26
BPF_MAP_TYPE_RINGBUF = 27
BPF_PROG_TYPE_STRUCT_OPS = 27
BPF_STRUCT_OPS = 44
BPF_F_MMAPABLE = 1 << 10
BPF_F_LINK = 1 << 13
BPF_PSEUDO_MAP_FD = 1
BPF_PSEUDO_KFUNC_CALL = 2
BTF_KIND_STRUCT = 4
BTF_KIND_FUNC = 12

FUNC_MAP_LOOKUP_ELEM = 1
FUNC_RINGBUF_RESERVE = 131
FUNC_RINGBUF_SUBMIT = 132

LDX_B, LDX_W, LDX_DW = 0x71, 0x61, 0x79
STX_B, STX_W, STX_DW = 0x73, 0x63, 0x7B
ST_W = 0x62
ADD_K, AND_K, OR_K, OR_X = 0x07, 0x57, 0x47, 0x4F
MOV_K, MOV_X = 0xB7, 0xBF
JEQ_K, JNE_K, JEQ_X = 0x15, 0x55, 0x1D
CALL, EXIT = 0x85, 0x95
LD_IMM64 = 0x18

RING_SIZE = 4096
STATE_SIZE = 64
# Programs count recognized reports here; a stalled count means the program detached.
REPORTS_OFFSET = 0
DRIVER_STATE_OFFSET = 8
VMLINUX_BTF = Path("/sys/kernel/btf/vmlinux")
LOG_SIZE = 1 << 20

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


@dataclass(frozen=True)
class ProgramContext:
    get_data_btf_id: int
    state_fd: int
    ring_fd: int


class HidBpfDriver(InputDriver, Protocol):
    def program(self, context: ProgramContext) -> bytes: ...


@dataclass(frozen=True)
class Jump:
    code: int
    dst: int
    src: int = 0
    imm: int = 0
    target: str = "exit"


@dataclass(frozen=True)
class Label:
    name: str


type Instruction = bytes | Jump | Label


def insn(code: int, dst: int = 0, src: int = 0, off: int = 0, imm: int = 0) -> bytes:
    return struct.pack("<BBhi", code, (src << 4) | dst, off, imm)


def ld_map_fd(dst: int, fd: int) -> bytes:
    return insn(LD_IMM64, dst, BPF_PSEUDO_MAP_FD, 0, fd) + insn(0)


def assemble(parts: Sequence[Instruction]) -> bytes:
    labels: dict[str, int] = {}
    position = 0
    for part in parts:
        if isinstance(part, Label):
            labels[part.name] = position
        elif isinstance(part, Jump):
            position += 1
        else:
            position += len(part) // 8
    code = bytearray()
    for part in parts:
        if isinstance(part, Label):
            continue
        if isinstance(part, Jump):
            offset = labels[part.target] - len(code) // 8 - 1
            code += insn(part.code, part.dst, part.src, offset, part.imm)
        else:
            code += part
    return bytes(code)


def bpf(command: int, attr: bytes) -> int:
    if SYS_BPF is None:
        raise OSError(errno.ENOSYS, f"HID-BPF loading is not supported on {platform.machine()}")
    buffer = ctypes.create_string_buffer(attr, 160)
    result = int(
        _libc.syscall(
            ctypes.c_long(SYS_BPF), ctypes.c_long(command), buffer, ctypes.c_long(len(buffer))
        )
    )
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, f"bpf command {command} failed: {os.strerror(error)}")
    return result


class KernelBtf:
    """Type lookup in the kernel's BTF, enough to resolve HID-BPF identifiers."""

    _extra = {1: 4, 3: 12, 14: 4, 17: 4}
    _per_member = {4: 12, 5: 12, 6: 8, 13: 8, 15: 12, 19: 12}

    def __init__(self, data: bytes) -> None:
        self.data = data
        magic, _version, _flags, header, type_off, type_len, str_off, _str_len = struct.unpack_from(
            "<HBBIIIII", data
        )
        if magic != 0xEB9F:
            raise ValueError("Kernel BTF has an unknown format")
        self.strings = header + str_off
        self.types: dict[int, int] = {}
        position, end, type_id = header + type_off, header + type_off + type_len, 1
        while position < end:
            _name, info, _size = struct.unpack_from("<III", data, position)
            kind, members = (info >> 24) & 0x1F, info & 0xFFFF
            self.types[type_id] = position
            position += 12 + self._extra.get(kind, 0) + self._per_member.get(kind, 0) * members
            type_id += 1

    def name(self, offset: int) -> str:
        start = self.strings + offset
        return self.data[start : self.data.index(b"\0", start)].decode()

    def find(self, kind: int, name: str) -> int:
        encoded = name.encode()
        for type_id, position in self.types.items():
            name_off, info, _ = struct.unpack_from("<III", self.data, position)
            if (info >> 24) & 0x1F != kind:
                continue
            start = self.strings + name_off
            if self.data[start : start + len(encoded) + 1] == encoded + b"\0":
                return type_id
        raise LookupError(name)

    def members(self, type_id: int) -> tuple[int, list[tuple[str, int]]]:
        position = self.types[type_id]
        _, info, size = struct.unpack_from("<III", self.data, position)
        result: list[tuple[str, int]] = []
        for index in range(info & 0xFFFF):
            name_off, _type, offset = struct.unpack_from(
                "<III", self.data, position + 12 + 12 * index
            )
            bits = offset & 0xFFFFFF if info >> 31 else offset
            result.append((self.name(name_off), bits // 8))
        return size, result


@dataclass(frozen=True)
class HidBpfLayout:
    ops_type_id: int
    value_type_id: int
    value_size: int
    data_offset: int
    hid_id_offset: int
    event_offset: int
    event_member: int
    get_data_btf_id: int

    @classmethod
    def from_btf(cls, btf: KernelBtf) -> "HidBpfLayout":
        try:
            ops = btf.find(BTF_KIND_STRUCT, "hid_bpf_ops")
            value = btf.find(BTF_KIND_STRUCT, "bpf_struct_ops_hid_bpf_ops")
            get_data = btf.find(BTF_KIND_FUNC, "hid_bpf_get_data")
        except LookupError as exc:
            raise OSError(
                "This kernel has no built-in HID-BPF support (CONFIG_HID_BPF, kernel 6.11+)"
            ) from exc
        _, ops_members = btf.members(ops)
        value_size, value_members = btf.members(value)
        names = [name for name, _ in ops_members]
        offsets = dict(ops_members)
        return cls(
            ops_type_id=ops,
            value_type_id=value,
            value_size=value_size,
            data_offset=dict(value_members)["data"],
            hid_id_offset=offsets["hid_id"],
            event_offset=offsets["hid_device_event"],
            event_member=names.index("hid_device_event"),
            get_data_btf_id=get_data,
        )


def _minimal_btf() -> bytes:
    # struct_ops maps require some program BTF even though their value type is the kernel's.
    types = struct.pack("<IIII", 1, 1 << 24, 4, (1 << 24) | 32)
    strings = b"\0int\0"
    header = struct.pack("<HBBIIIII", 0xEB9F, 1, 0, 24, 0, len(types), len(types), len(strings))
    return header + types + strings


def _map_create(
    map_type: int,
    key_size: int,
    value_size: int,
    entries: int,
    flags: int = 0,
    *,
    name: bytes,
    btf_fd: int = 0,
    vmlinux_value_type_id: int = 0,
) -> int:
    attr = struct.pack(
        "<7I16s5I",
        map_type,
        key_size,
        value_size,
        entries,
        flags,
        0,
        0,
        name,
        0,
        btf_fd,
        0,
        0,
        vmlinux_value_type_id,
    )
    return bpf(BPF_MAP_CREATE, attr)


def _address(buffer: ctypes.Array[ctypes.c_char]) -> int:
    return ctypes.addressof(buffer)


def _load_program(code: bytes, layout: HidBpfLayout, name: bytes) -> int:
    instructions = ctypes.create_string_buffer(code, len(code))
    license_ = ctypes.create_string_buffer(b"GPL")
    log = ctypes.create_string_buffer(LOG_SIZE)

    def load(log_level: int) -> int:
        attr = struct.pack(
            "<IIQQIIQII16sIIIIQIIQIIII",
            BPF_PROG_TYPE_STRUCT_OPS,
            len(code) // 8,
            _address(instructions),
            _address(license_),
            log_level,
            LOG_SIZE if log_level else 0,
            _address(log) if log_level else 0,
            0,
            0,
            name,
            0,
            layout.event_member,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            layout.ops_type_id,
            0,
            0,
        )
        return bpf(BPF_PROG_LOAD, attr)

    try:
        return load(0)
    except OSError as exc:
        try:
            os.close(load(1))
        except OSError:
            detail = log.value.decode(errors="replace").strip()
            raise OSError(exc.errno, f"HID-BPF program rejected: {detail or exc}") from exc
        raise


@dataclass(frozen=True)
class AttachedProgram:
    link_fd: int
    ring_fd: int
    state_fd: int

    @property
    def fds(self) -> tuple[int, int, int]:
        return self.link_fd, self.ring_fd, self.state_fd

    def close(self) -> None:
        for fd in self.fds:
            os.close(fd)


def attach(driver: HidBpfDriver, hid_id: int, *, btf: KernelBtf | None = None) -> AttachedProgram:
    """Attach a driver's program to one HID device; closing the link fd detaches it."""
    layout = HidBpfLayout.from_btf(btf or KernelBtf(VMLINUX_BTF.read_bytes()))
    name = driver.id.replace("-", "_").encode()[:15]
    opened: list[int] = []
    try:
        ring_fd = _map_create(BPF_MAP_TYPE_RINGBUF, 0, 0, RING_SIZE, name=name[:12] + b"_ev")
        opened.append(ring_fd)
        state_fd = _map_create(
            BPF_MAP_TYPE_ARRAY, 4, STATE_SIZE, 1, BPF_F_MMAPABLE, name=name[:12] + b"_st"
        )
        opened.append(state_fd)
        code = driver.program(ProgramContext(layout.get_data_btf_id, state_fd, ring_fd))
        prog_fd = _load_program(code, layout, name)
        opened.append(prog_fd)
        blob = ctypes.create_string_buffer(_minimal_btf(), len(_minimal_btf()))
        btf_fd = bpf(BPF_BTF_LOAD, struct.pack("<QQII", _address(blob), 0, len(blob), 0))
        opened.append(btf_fd)
        ops_fd = _map_create(
            BPF_MAP_TYPE_STRUCT_OPS,
            4,
            layout.value_size,
            1,
            BPF_F_LINK,
            name=name,
            btf_fd=btf_fd,
            vmlinux_value_type_id=layout.value_type_id,
        )
        opened.append(ops_fd)
        value = bytearray(layout.value_size)
        struct.pack_into("<i", value, layout.data_offset + layout.hid_id_offset, hid_id)
        struct.pack_into("<Q", value, layout.data_offset + layout.event_offset, prog_fd)
        key = ctypes.create_string_buffer(struct.pack("<I", 0), 4)
        value_buffer = ctypes.create_string_buffer(bytes(value), len(value))
        bpf(
            BPF_MAP_UPDATE_ELEM,
            struct.pack("<IIQQQ", ops_fd, 0, _address(key), _address(value_buffer), 0),
        )
        link_fd = bpf(BPF_LINK_CREATE, struct.pack("<IIII", ops_fd, 0, BPF_STRUCT_OPS, 0))
    except BaseException:
        for fd in opened:
            os.close(fd)
        raise
    for fd in (prog_fd, btf_fd, ops_fd):
        os.close(fd)
    return AttachedProgram(link_fd, ring_fd, state_fd)
