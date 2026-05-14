import struct

from keymasq.common.ipc import (
    HEADER_FORMAT,
    Command,
    CommandType,
    Response,
    decode_command,
    decode_response,
    encode_command,
    encode_response,
)


class TestProtocolEncoding:
    def test_encode_decode_command(self):
        cmd = Command(
            command=CommandType.GRAB_DEVICE,
            data={"hardware_id": "1234:5678", "evdev_paths": ["/dev/input/event5"]},
            request_id="test-123",
        )

        encoded = encode_command(cmd)
        assert isinstance(encoded, bytes)
        assert len(encoded) > 4

        decoded, remaining = decode_command(encoded)

        assert decoded is not None
        assert decoded.command == CommandType.GRAB_DEVICE
        assert decoded.data["hardware_id"] == "1234:5678"
        assert decoded.request_id == "test-123"
        assert remaining == b""

    def test_encode_decode_response(self):
        resp = Response(
            status="ok",
            data={"grabbed": True},
            request_id="test-456",
        )

        encoded = encode_response(resp)
        assert isinstance(encoded, bytes)

        decoded, remaining = decode_response(encoded)

        assert decoded is not None
        assert decoded.status == "ok"
        assert decoded.data["grabbed"] is True
        assert decoded.request_id == "test-456"
        assert remaining == b""

    def test_decode_partial_data(self):
        cmd = Command(command=CommandType.PING, data={})
        encoded = encode_command(cmd)

        partial = encoded[:10]
        decoded, remaining = decode_command(partial)

        assert decoded is None
        assert remaining == partial

    def test_decode_multiple_commands(self):
        cmd1 = Command(command=CommandType.PING, data={}, request_id="1")
        cmd2 = Command(command=CommandType.LIST_DEVICES, data={}, request_id="2")

        encoded = encode_command(cmd1) + encode_command(cmd2)

        decoded1, remaining1 = decode_command(encoded)
        assert decoded1 is not None
        assert decoded1.request_id == "1"

        decoded2, remaining2 = decode_command(remaining1)
        assert decoded2 is not None
        assert decoded2.request_id == "2"
        assert remaining2 == b""

    def test_decode_oversized_command_discards_full_frame(self, monkeypatch):
        import keymasq.common.ipc as ipc

        valid = encode_command(Command(command=CommandType.PING, data={}, request_id="ok"))
        max_payload_size = struct.unpack(HEADER_FORMAT, valid[: ipc.HEADER_SIZE])[0]
        monkeypatch.setattr(ipc, "MAX_PAYLOAD_SIZE", max_payload_size)
        payload_len = max_payload_size + 1
        oversized = struct.pack(HEADER_FORMAT, payload_len) + (b"x" * payload_len)

        decoded, remaining = decode_command(oversized + valid)

        assert decoded is None
        assert remaining == valid

        decoded, remaining = decode_command(remaining)
        assert decoded is not None
        assert decoded.request_id == "ok"
        assert remaining == b""

    def test_decode_partial_oversized_command_discards_buffer(self, monkeypatch):
        import keymasq.common.ipc as ipc

        monkeypatch.setattr(ipc, "MAX_PAYLOAD_SIZE", 8)
        partial = struct.pack(HEADER_FORMAT, 9) + b"xxx"

        decoded, remaining = decode_command(partial)

        assert decoded is None
        assert remaining == b""

    def test_error_response(self):
        resp = Response(
            status="error",
            error="Device not found",
            request_id="err-1",
        )

        encoded = encode_response(resp)
        decoded, _ = decode_response(encoded)

        assert decoded.status == "error"
        assert decoded.error == "Device not found"

    def test_decode_oversized_response_discards_full_frame(self, monkeypatch):
        import keymasq.common.ipc as ipc

        valid = encode_response(Response(status="ok", request_id="ok"))
        max_payload_size = struct.unpack(HEADER_FORMAT, valid[: ipc.HEADER_SIZE])[0]
        monkeypatch.setattr(ipc, "MAX_PAYLOAD_SIZE", max_payload_size)
        payload_len = max_payload_size + 1
        oversized = struct.pack(HEADER_FORMAT, payload_len) + (b"x" * payload_len)

        decoded, remaining = decode_response(oversized + valid)

        assert decoded is None
        assert remaining == valid

        decoded, remaining = decode_response(remaining)
        assert decoded is not None
        assert decoded.request_id == "ok"
        assert remaining == b""

    def test_decode_partial_oversized_response_discards_buffer(self, monkeypatch):
        import keymasq.common.ipc as ipc

        monkeypatch.setattr(ipc, "MAX_PAYLOAD_SIZE", 8)
        partial = struct.pack(HEADER_FORMAT, 9) + b"xxx"

        decoded, remaining = decode_response(partial)

        assert decoded is None
        assert remaining == b""


class TestCommandTypes:
    def test_all_command_types_have_value(self):
        for ct in CommandType:
            assert isinstance(ct.value, str)
            assert len(ct.value) > 0

    def test_command_type_values(self):
        assert CommandType.GRAB_DEVICE.value == "grab_device"
        assert CommandType.RELEASE_DEVICE.value == "release_device"
        assert CommandType.SET_MAPPING.value == "set_mapping"
        assert CommandType.SET_COMBOS.value == "set_combos"
        assert CommandType.LIST_DEVICES.value == "list_devices"
        assert CommandType.PING.value == "ping"
        assert CommandType.DEVICE_EVENT.value == "device_event"
        assert CommandType.ACTION_TRIGGER.value == "action_trigger"
