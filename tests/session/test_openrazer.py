from types import SimpleNamespace
from typing import cast

import pytest

from keymasq.session.manager import openrazer
from keymasq.session.manager.state import OpenRazerRuntimeState


class _FakeInterface:
    def __init__(self) -> None:
        self.devices = ["ABC123"]
        self.vid_pid = [0x1532, 0x0098]
        self.name = "Razer Test Mouse"
        self.device_type = "mouse"
        self.dpi = [800, 1200]
        self.max_dpi = 30000
        self.available_dpi: list[int] = []
        self.poll_rate = 1000
        self.supported_poll_rates: list[int] | None = [125, 500, 1000]
        self.set_dpi_calls: list[tuple[int, int]] = []
        self.set_poll_rate_calls: list[int] = []

    async def call_get_devices(self):
        return self.devices

    async def call_get_vid_pid(self):
        return self.vid_pid

    async def call_get_device_name(self):
        return self.name

    async def call_get_device_type(self):
        return self.device_type

    async def call_get_dpi(self):
        return self.dpi

    async def call_set_dpi(self, x: int, y: int):
        self.set_dpi_calls.append((int(x), int(y)))
        self.dpi = [int(x), int(y)]

    async def call_max_dpi(self):
        return self.max_dpi

    async def call_available_dpi(self):
        return self.available_dpi

    async def call_get_poll_rate(self):
        return self.poll_rate

    async def call_set_poll_rate(self, rate: int):
        self.set_poll_rate_calls.append(int(rate))
        self.poll_rate = int(rate)

    async def call_get_supported_poll_rates(self):
        return self.supported_poll_rates


class _FakeDBus:
    def __init__(
        self,
        iface: _FakeInterface,
        *,
        has_owner: bool = True,
        supported_poll_rates_method: bool = True,
        bus: object | None = None,
    ) -> None:
        self.iface = iface
        self.has_owner = has_owner
        self.supported_poll_rates_method = supported_poll_rates_method
        self.session_bus = bus

    async def name_has_owner(self, name: str, *, timeout: float = 0.6) -> bool:
        assert name == openrazer.OPENRAZER_SERVICE
        return self.has_owner

    async def bus(self):
        if self.session_bus is None:
            raise AssertionError("session bus not configured")
        return self.session_bus

    async def get_interface(self, destination: str, path: str, interface: str):
        assert destination == openrazer.OPENRAZER_SERVICE
        return self.iface

    async def introspect(self, destination: str, path: str):
        assert destination == openrazer.OPENRAZER_SERVICE
        misc_methods = [
            "getVidPid",
            "getDeviceName",
            "getDeviceType",
            "getPollRate",
            "setPollRate",
        ]
        if self.supported_poll_rates_method:
            misc_methods.append("getSupportedPollRates")
        methods_by_interface = {
            openrazer.OPENRAZER_MISC_INTERFACE: misc_methods,
            openrazer.OPENRAZER_DPI_INTERFACE: [
                "getDPI",
                "setDPI",
                "maxDPI",
            ],
        }
        return SimpleNamespace(
            interfaces=[
                SimpleNamespace(
                    name=interface_name,
                    methods=[SimpleNamespace(name=name) for name in method_names],
                )
                for interface_name, method_names in methods_by_interface.items()
            ]
        )


class _FakeBus:
    def __init__(self) -> None:
        self.handlers: list[object] = []
        self.calls: list[tuple[str, str]] = []

    async def call(self, message):
        self.calls.append((str(message.member), str(message.body[0])))
        return None

    def add_message_handler(self, handler) -> None:
        self.handlers.append(handler)

    def remove_message_handler(self, handler) -> None:
        self.handlers.remove(handler)


def _manager(fake_dbus: _FakeDBus):
    return SimpleNamespace(
        dbus=fake_dbus,
        openrazer_state=OpenRazerRuntimeState(),
    )


@pytest.mark.asyncio
async def test_refresh_openrazer_loads_devices_without_openrazer_dependency() -> None:
    iface = _FakeInterface()
    manager = _manager(_FakeDBus(iface))

    status = await openrazer.refresh_openrazer(manager, force=True)

    assert status["available"] is True
    assert status["devices"] == [
        {
            "serial": "ABC123",
            "name": "Razer Test Mouse",
            "device_type": "mouse",
            "vendor_id": "1532",
            "product_id": "0098",
            "hardware_id": "1532:0098",
            "has_dpi": True,
            "has_available_dpi": False,
            "has_poll_rate": True,
            "has_supported_poll_rates": True,
            "dpi": [800, 1200],
            "max_dpi": 30000,
            "available_dpi": [],
            "poll_rate": 1000,
            "supported_poll_rates": [125, 500, 1000],
            "poll_rate_templates": [125, 500, 1000],
            "poll_rate_templates_source": "openrazer",
        }
    ]


@pytest.mark.asyncio
async def test_refresh_openrazer_defaults_poll_templates_without_dbus_list() -> None:
    iface = _FakeInterface()
    iface.supported_poll_rates = None
    manager = _manager(_FakeDBus(iface, supported_poll_rates_method=False))

    status = await openrazer.refresh_openrazer(manager, force=True)

    assert status["available"] is True
    devices = cast(list[dict[str, object]], status["devices"])
    device = devices[0]
    assert device["has_supported_poll_rates"] is False
    assert "supported_poll_rates" not in device
    assert device["poll_rate_templates"] == [125, 500, 1000]
    assert device["poll_rate_templates_source"] == "default"


@pytest.mark.asyncio
async def test_openrazer_monitor_removes_dbus_match_rule_on_stop() -> None:
    bus = _FakeBus()
    manager = _manager(_FakeDBus(_FakeInterface(), bus=bus))

    await openrazer.start_openrazer_monitor(manager)

    assert manager.openrazer_state.watcher_installed is True
    assert manager.openrazer_state.watcher_match_rule == openrazer.OPENRAZER_NAME_OWNER_MATCH_RULE
    assert bus.calls == [
        ("AddMatch", openrazer.OPENRAZER_NAME_OWNER_MATCH_RULE),
    ]
    assert len(bus.handlers) == 1

    await openrazer.stop_openrazer_monitor(manager)

    assert manager.openrazer_state.watcher_installed is False
    assert manager.openrazer_state.watcher_handler is None
    assert manager.openrazer_state.watcher_match_rule is None
    assert bus.handlers == []
    assert bus.calls == [
        ("AddMatch", openrazer.OPENRAZER_NAME_OWNER_MATCH_RULE),
        ("RemoveMatch", openrazer.OPENRAZER_NAME_OWNER_MATCH_RULE),
    ]


@pytest.mark.asyncio
async def test_openrazer_action_sets_dpi_pair() -> None:
    iface = _FakeInterface()
    manager = _manager(_FakeDBus(iface))
    await openrazer.refresh_openrazer(manager, force=True)

    result = await openrazer.handle_openrazer_action(
        manager,
        {
            "setting": "dpi",
            "serial": "ABC123",
            "dpi_x": 1600,
            "dpi_y": 1200,
        },
    )

    assert result == {
        "status": "ok",
        "serial": "ABC123",
        "setting": "dpi",
        "dpi": [1600, 1200],
    }
    assert iface.set_dpi_calls == [(1600, 1200)]


@pytest.mark.asyncio
async def test_openrazer_action_keeps_split_dpi_when_available_dpi_exists() -> None:
    iface = _FakeInterface()
    manager = _manager(_FakeDBus(iface))
    await openrazer.refresh_openrazer(manager, force=True)
    manager.openrazer_state.devices["ABC123"]["has_available_dpi"] = True
    manager.openrazer_state.devices["ABC123"]["available_dpi"] = [800, 1200, 1600]

    result = await openrazer.handle_openrazer_action(
        manager,
        {
            "setting": "dpi",
            "serial": "ABC123",
            "dpi_x": 1600,
            "dpi_y": 1200,
        },
    )

    assert result == {
        "status": "ok",
        "serial": "ABC123",
        "setting": "dpi",
        "dpi": [1600, 1200],
    }
    assert iface.set_dpi_calls == [(1600, 1200)]


@pytest.mark.asyncio
async def test_openrazer_action_uses_single_axis_dpi_when_device_reports_one_value() -> None:
    iface = _FakeInterface()
    iface.dpi = [800]
    manager = _manager(_FakeDBus(iface))
    await openrazer.refresh_openrazer(manager, force=True)

    result = await openrazer.handle_openrazer_action(
        manager,
        {
            "setting": "dpi",
            "serial": "ABC123",
            "dpi_x": 1600,
            "dpi_y": 1200,
        },
    )

    assert result == {
        "status": "ok",
        "serial": "ABC123",
        "setting": "dpi",
        "dpi": [1600, 0],
    }
    assert iface.set_dpi_calls == [(1600, 0)]


@pytest.mark.asyncio
async def test_openrazer_action_validates_available_dpi_y() -> None:
    iface = _FakeInterface()
    manager = _manager(_FakeDBus(iface))
    await openrazer.refresh_openrazer(manager, force=True)
    manager.openrazer_state.devices["ABC123"]["available_dpi"] = [800, 1600]

    result = await openrazer.handle_openrazer_action(
        manager,
        {
            "setting": "dpi",
            "serial": "ABC123",
            "dpi_x": 1600,
            "dpi_y": 1200,
        },
    )

    assert result["status"] == "error"
    assert "DPI Y 1200" in str(result["message"])
    assert iface.set_dpi_calls == []


@pytest.mark.asyncio
async def test_openrazer_action_sets_poll_rate() -> None:
    iface = _FakeInterface()
    manager = _manager(_FakeDBus(iface))
    await openrazer.refresh_openrazer(manager, force=True)

    result = await openrazer.handle_openrazer_action(
        manager,
        {
            "setting": "poll_rate",
            "serial": "ABC123",
            "poll_rate": 500,
        },
    )

    assert result == {
        "status": "ok",
        "serial": "ABC123",
        "setting": "poll_rate",
        "poll_rate": 500,
    }
    assert iface.set_poll_rate_calls == [500]


@pytest.mark.asyncio
async def test_openrazer_action_allows_custom_poll_rate() -> None:
    iface = _FakeInterface()
    manager = _manager(_FakeDBus(iface))
    await openrazer.refresh_openrazer(manager, force=True)

    result = await openrazer.handle_openrazer_action(
        manager,
        {
            "setting": "poll_rate",
            "serial": "ABC123",
            "poll_rate": 2000,
        },
    )

    assert result == {
        "status": "ok",
        "serial": "ABC123",
        "setting": "poll_rate",
        "poll_rate": 2000,
    }
    assert iface.set_poll_rate_calls == [2000]


@pytest.mark.asyncio
async def test_openrazer_unavailable_is_reported_without_importing_openrazer() -> None:
    iface = _FakeInterface()
    manager = _manager(_FakeDBus(iface, has_owner=False))

    status = await openrazer.refresh_openrazer(manager, force=True)

    assert status["available"] is False
    assert status["devices"] == []
    assert "not running" in str(status["last_error"])
