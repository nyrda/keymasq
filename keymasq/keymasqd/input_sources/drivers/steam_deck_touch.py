"""Steam Deck stick and trackpad touch, which hid-steam reads but never reports.

Layout from hid-steam's Deck report table and SDL's SteamDeckStatePacket_t:
report 0x09, stick touch in byte 13 bits 6 and 7, pad touch in byte 10 bits 3 and 4.
"""

import struct
from typing import Literal

from ..bpf import (
    ADD_K,
    AND_K,
    BPF_PSEUDO_KFUNC_CALL,
    CALL,
    DRIVER_STATE_OFFSET,
    DRIVER_STATE_SIZE,
    EXIT,
    FUNC_MAP_LOOKUP_ELEM,
    FUNC_RINGBUF_RESERVE,
    FUNC_RINGBUF_SUBMIT,
    JEQ_K,
    JEQ_X,
    JNE_K,
    LDX_B,
    LDX_DW,
    MOV_K,
    MOV_X,
    OR_K,
    OR_X,
    RECORD_SIZE,
    REPORTS_OFFSET,
    SEQUENCE_OFFSET,
    ST_W,
    STX_DW,
    Jump,
    Label,
    ProgramContext,
    assemble,
    insn,
    ld_map_fd,
)
from ..types import Channel, Endpoint

REPORT_SIZE = 64
DECK_STATE_REPORT = 0x09
STICK_TOUCH_BYTE = 13
LEFT_STICK_TOUCH = 0x40
RIGHT_STICK_TOUCH = 0x80
PAD_TOUCH_BYTE = 10
LEFT_PAD_TOUCH = 0x08
RIGHT_PAD_TOUCH = 0x10
VALID = 0x01
HID_GROUP_GENERIC = 0x0001
FEATURE_ITEM = 0xB0


def has_feature_report(descriptor: bytes) -> bool:
    position = 0
    while position < len(descriptor):
        prefix = descriptor[position]
        if prefix == 0xFE:
            if position + 1 >= len(descriptor):
                return False
            position += 3 + descriptor[position + 1]
            continue
        if prefix & 0xFC == FEATURE_ITEM:
            return True
        position += 1 + (4 if prefix & 3 == 3 else prefix & 3)
    return False


class SteamDeckTouchDriver:
    id = "steam-deck-touch"
    label = "Steam Deck touch"
    association: Literal["same_hid", "same_usb"] = "same_hid"
    transport: Literal["hidraw", "hid-bpf"] = "hid-bpf"
    models = frozenset({(0x28DE, 0x1205)})
    channels = (
        Channel("left_stick_touch", "button", "btn_touch_ls"),
        Channel("right_stick_touch", "button", "btn_touch_rs"),
        Channel("left_pad_touch", "button", "btn_touch_lp"),
        Channel("right_pad_touch", "button", "btn_touch_rp"),
    )

    def matches(self, endpoint: Endpoint) -> bool:
        # Only the gamepad interface has feature reports; hid-steam's client has its own group.
        return (
            endpoint.bus == 3
            and (endpoint.vendor, endpoint.product) == (0x28DE, 0x1205)
            and endpoint.group == HID_GROUP_GENERIC
            and has_feature_report(endpoint.descriptor)
        )

    def decode(self, report: bytes) -> dict[str, int] | None:
        if len(report) != DRIVER_STATE_SIZE:
            return None
        touch = struct.unpack_from("<Q", report)[0]
        if not touch & VALID:
            return None
        return {
            "left_stick_touch": int(bool(touch & LEFT_STICK_TOUCH)),
            "right_stick_touch": int(bool(touch & RIGHT_STICK_TOUCH)),
            "left_pad_touch": int(bool(touch & LEFT_PAD_TOUCH)),
            "right_pad_touch": int(bool(touch & RIGHT_PAD_TOUCH)),
        }

    def program(self, context: ProgramContext) -> bytes:
        """Count state reports, keep the latest touch bits, and record each change in order."""
        header = [
            part
            for offset, expected in ((0, 0x01), (1, 0x00), (2, DECK_STATE_REPORT))
            for part in (insn(LDX_B, 1, 7, offset), Jump(JNE_K, 1, imm=expected))
        ]
        return assemble(
            [
                insn(LDX_DW, 6, 1, 0),
                insn(MOV_X, 1, 6),
                insn(MOV_K, 2, imm=0),
                insn(MOV_K, 3, imm=REPORT_SIZE),
                insn(CALL, src=BPF_PSEUDO_KFUNC_CALL, imm=context.get_data_btf_id),
                Jump(JEQ_K, 0),
                insn(MOV_X, 7, 0),
                *header,
                insn(LDX_B, 8, 7, STICK_TOUCH_BYTE),
                insn(AND_K, 8, imm=LEFT_STICK_TOUCH | RIGHT_STICK_TOUCH),
                insn(LDX_B, 1, 7, PAD_TOUCH_BYTE),
                insn(AND_K, 1, imm=LEFT_PAD_TOUCH | RIGHT_PAD_TOUCH),
                insn(OR_X, 8, 1),
                insn(OR_K, 8, imm=VALID),
                insn(ST_W, 10, off=-4, imm=0),
                ld_map_fd(1, context.state_fd),
                insn(MOV_X, 2, 10),
                insn(ADD_K, 2, imm=-4),
                insn(CALL, imm=FUNC_MAP_LOOKUP_ELEM),
                Jump(JEQ_K, 0),
                insn(MOV_X, 9, 0),
                insn(LDX_DW, 1, 9, REPORTS_OFFSET),
                insn(ADD_K, 1, imm=1),
                insn(STX_DW, 9, 1, REPORTS_OFFSET),
                insn(LDX_DW, 1, 9, DRIVER_STATE_OFFSET),
                Jump(JEQ_X, 1, src=8),
                insn(STX_DW, 9, 8, DRIVER_STATE_OFFSET),
                insn(LDX_DW, 6, 9, SEQUENCE_OFFSET),
                insn(ADD_K, 6, imm=1),
                insn(STX_DW, 9, 6, SEQUENCE_OFFSET),
                ld_map_fd(1, context.ring_fd),
                insn(MOV_K, 2, imm=RECORD_SIZE),
                insn(MOV_K, 3, imm=0),
                insn(CALL, imm=FUNC_RINGBUF_RESERVE),
                Jump(JEQ_K, 0),
                insn(STX_DW, 0, 6, 0),
                insn(STX_DW, 0, 8, 8),
                insn(MOV_X, 1, 0),
                insn(MOV_K, 2, imm=0),
                insn(CALL, imm=FUNC_RINGBUF_SUBMIT),
                Label("exit"),
                insn(MOV_K, 0, imm=0),
                insn(EXIT),
            ]
        )
