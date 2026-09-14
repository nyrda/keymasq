from types import SimpleNamespace

import pytest

from keymasq.common.ipc import CommandType
from keymasq.keymasqd.daemon_device_commands import handle_device_command
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.runtime.action_parser import parse_action


@pytest.mark.asyncio
async def test_unused_reference_query_tracks_pattern_actions_and_duplicates():
    manager = DeviceManager()
    daemon = SimpleNamespace(device_manager=manager)
    action = parse_action(
        manager,
        {
            "action": "superkey",
            "superkey": {
                "name": "tap",
                "mode": "pattern",
                "tap_actions": [{"action": "exec", "exec_ref": 7}],
            },
        },
    )
    duplicate = parse_action(manager, {"action": "exec", "exec_ref": 7})
    result = await handle_device_command(
        daemon, CommandType.UNUSED_EXEC_REFS, {"exec_refs": [7, 8]}
    )
    assert result == {"unused_exec_refs": [8]}
    del duplicate
    assert manager.exec_references.unused([7]) == []
    del action
    assert manager.exec_references.unused([7]) == [7]


@pytest.mark.asyncio
@pytest.mark.parametrize("refs", [None, "7", [True], ["7"], [1.5]])
async def test_unused_reference_query_rejects_invalid_ids(refs):
    daemon = SimpleNamespace(device_manager=DeviceManager())
    with pytest.raises(ValueError, match="integers"):
        await handle_device_command(daemon, CommandType.UNUSED_EXEC_REFS, {"exec_refs": refs})
