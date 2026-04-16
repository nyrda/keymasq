from keymasq.common.ipc import (
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
