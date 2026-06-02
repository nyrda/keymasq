import asyncio
import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest import mock

import evdev
import pytest

from keymasq.common.models import (
    ActionType,
    ButtonDefinition,
    DeviceProfileLayer,
    DeviceType,
    EvdevDevice,
    HardwareConfig,
    MappingAction,
    ProfileConfig,
)

TEST_UINPUT_ENV = "KEYMASQ_TEST_UINPUT"
TEST_UINPUT_PREFIX = "keymasq-test"


def _create_virtual_uinput(
    *,
    capabilities: dict[int, list[int]],
    name: str,
    vendor: int,
    product: int,
) -> evdev.UInput:
    try:
        device = evdev.UInput(
            events=capabilities,
            name=name,
            vendor=vendor,
            product=product,
        )
    except Exception as exc:
        pytest.skip(f"Virtual uinput device unavailable: {exc}")

    backing_device = getattr(device, "device", None)
    if not getattr(backing_device, "path", None):
        device.close()
        pytest.skip("Virtual uinput device path unavailable")

    return device


@pytest.fixture(autouse=True)
def enable_test_uinput_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TEST_UINPUT_ENV, "1")


@pytest.fixture(autouse=True)
def isolate_session_recording_settings_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from keymasq.session.manager.core import SessionManager

    monkeypatch.setattr(
        SessionManager,
        "RECORDING_SETTINGS_PATH",
        tmp_path / "recording_settings.toml",
    )
    monkeypatch.setattr(
        "keymasq.keymasqd.recording.STATE_DIR",
        tmp_path / "state",
    )


@pytest.fixture
def temp_config_dir(tmp_path: Path) -> Generator[Path, None, None]:
    config_dir = tmp_path / "keymasq"
    hardware_dir = config_dir / "hardware"
    profiles_dir = config_dir / "profiles"
    analog_controls_dir = config_dir / "analog_controls"
    hardware_dir.mkdir(parents=True)
    profiles_dir.mkdir(parents=True)
    analog_controls_dir.mkdir(parents=True)

    with (
        mock.patch("keymasq.common.paths.CONFIG_DIR", config_dir),
        mock.patch("keymasq.common.paths.HARDWARE_DIR", hardware_dir),
        mock.patch("keymasq.common.paths.PROFILES_DIR", profiles_dir),
        mock.patch("keymasq.common.paths.ANALOG_CONTROLS_DIR", analog_controls_dir),
    ):
        yield config_dir


@pytest.fixture
def temp_socket_dir(tmp_path: Path) -> Generator[Path, None, None]:
    # Keep the UNIX socket path short enough for Linux AF_UNIX limits in CI.
    with tempfile.TemporaryDirectory(prefix="keymasq-sock-") as temp_dir:
        run_dir = Path(temp_dir)
        socket_path = run_dir / "socket"

        with (
            mock.patch("keymasq.common.paths.RUN_DIR", run_dir),
            mock.patch("keymasq.common.paths.SOCKET_PATH", socket_path),
        ):
            yield run_dir


@pytest.fixture
def virtual_mouse():
    capabilities = {
        evdev.ecodes.EV_KEY: [
            evdev.ecodes.BTN_LEFT,
            evdev.ecodes.BTN_RIGHT,
            evdev.ecodes.BTN_MIDDLE,
            evdev.ecodes.BTN_SIDE,
            evdev.ecodes.BTN_EXTRA,
        ],
        evdev.ecodes.EV_REL: [
            evdev.ecodes.REL_X,
            evdev.ecodes.REL_Y,
            evdev.ecodes.REL_WHEEL,
            evdev.ecodes.REL_HWHEEL,
        ],
    }

    device = _create_virtual_uinput(
        capabilities=capabilities,
        name=f"{TEST_UINPUT_PREFIX}-source-mouse",
        vendor=0x1234,
        product=0x5678,
    )

    yield device

    device.close()


@pytest.fixture
def virtual_keyboard():
    capabilities = {
        evdev.ecodes.EV_KEY: [
            evdev.ecodes.KEY_A,
            evdev.ecodes.KEY_B,
            evdev.ecodes.KEY_C,
            evdev.ecodes.KEY_1,
            evdev.ecodes.KEY_2,
            evdev.ecodes.KEY_F13,
            evdev.ecodes.KEY_F14,
            evdev.ecodes.KEY_SPACE,
            evdev.ecodes.KEY_LEFTALT,
            evdev.ecodes.KEY_LEFTCTRL,
        ],
    }

    device = _create_virtual_uinput(
        capabilities=capabilities,
        name=f"{TEST_UINPUT_PREFIX}-source-keyboard",
        vendor=0xABCD,
        product=0xEF01,
    )

    yield device

    device.close()


@pytest.fixture
def sample_hardware_config() -> HardwareConfig:
    return HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Test Mouse",
        evdev_devices=[
            EvdevDevice(
                path="/dev/input/event99",
                device_type=DeviceType.MOUSE,
                capabilities=["btn_left", "btn_right", "rel_wheel"],
            ),
        ],
        buttons=[
            ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left"),
            ButtonDefinition(id="btn_right", label="Right Click", evdev="btn_right"),
            ButtonDefinition(id="btn_middle", label="Middle Click", evdev="btn_middle"),
            ButtonDefinition(id="btn_back", label="Back", evdev="btn_side"),
            ButtonDefinition(id="btn_forward", label="Forward", evdev="btn_extra"),
            ButtonDefinition(
                id="wheel_up", label="Scroll Up", evdev="rel_wheel", evdev_value=1, type="wheel"
            ),
            ButtonDefinition(
                id="wheel_down",
                label="Scroll Down",
                evdev="rel_wheel",
                evdev_value=-1,
                type="wheel",
            ),
        ],
    )


@pytest.fixture
def sample_profile_config() -> ProfileConfig:
    return ProfileConfig(
        name="Test Profile",
        enabled=True,
        device_layers={
            "1234:5678": DeviceProfileLayer(
                hardware_id="1234:5678",
                mappings={
                    "btn_back": MappingAction(action_type=ActionType.KEYBOARD, target="key_1"),
                    "btn_forward": MappingAction(
                        action_type=ActionType.KEYBOARD,
                        target="key_2",
                    ),
                },
            ),
        },
    )


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


_CATEGORY_SUBTREES = {"common", "keymasqd", "session", "gui"}
_CATEGORY_BY_FILE = {
    "test_daemon.py": "keymasqd",
    "test_session_clients.py": "session",
}


def _category_for_test_path(item_path: Path) -> str | None:
    parts = item_path.parts
    for tests_index in range(len(parts) - 2, -1, -1):
        if parts[tests_index] != "tests":
            continue
        subtree = parts[tests_index + 1]
        if subtree in _CATEGORY_SUBTREES:
            return subtree
        if "tests" in parts[:tests_index]:
            return _CATEGORY_BY_FILE.get(item_path.name)
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    for item in items:
        item_path = Path(str(item.fspath))
        category = _category_for_test_path(item_path)
        if category is not None:
            item.add_marker(getattr(pytest.mark, category))
