"""Associate hardware configuration interfaces with physical masking targets."""

from collections.abc import Callable, Sequence
from pathlib import Path

from keymasq.common.devices import parse_hardware_model_id, parse_keymasq_device_path
from keymasq.common.model.hardware import HardwareConfig
from keymasq.gui.session_client import GuiTaskResult, run_gui_task
from keymasq.gui.widgets.hardware_masking_dialog import HardwareMaskingPanel
from keymasq.keymasqd.input_sources.discovery import SOURCE_PREFIX
from keymasq.masking.inventory import HardwareInventory, read_attribute


def hardware_masking_sources(config: HardwareConfig) -> list[tuple[str, str]]:
    return [(device.path, device.phys or "") for device in config.evdev_devices] + [
        (source.path, source.phys or "") for source in config.input_sources
    ]


def resolve_masking_devices(
    sources: Sequence[tuple[str, str]],
    inventory: HardwareInventory | None = None,
    *,
    hardware_id: str = "",
) -> set[str]:
    """Resolve source paths/phys without opening devices hidden from the session."""
    inventory = inventory or HardwareInventory()
    attachments = inventory.scan()
    events = list((inventory.sys_root / "class/input").glob("event*"))
    hid_devices = list((inventory.sys_root / "bus/hid/devices").glob("*"))
    identities: set[str] = set()
    configured_model = parse_hardware_model_id(hardware_id)
    for path, phys in sources:
        matched: set[str] = set()
        model = parse_keymasq_device_path(path)
        expected_model = configured_model or model
        resolved = Path(path).resolve() if path.startswith("/") else None
        for event in events:
            device = (event / "device").resolve()
            if resolved is not None and "/by-" in path:
                matches = resolved == inventory.dev_root / "input" / event.name
            elif phys:
                matches = read_attribute(device / "phys") == phys
            elif resolved is not None:
                matches = resolved == inventory.dev_root / "input" / event.name
            elif model:
                matches = (
                    read_attribute(device / "id/vendor").lower(),
                    read_attribute(device / "id/product").lower(),
                ) == model
            else:
                matches = False
            if matches and expected_model:
                matches = (
                    read_attribute(device / "id/vendor").lower(),
                    read_attribute(device / "id/product").lower(),
                ) == expected_model
            if matches:
                matched.update(
                    item.identity for item in attachments if device.is_relative_to(item.syspath)
                )
        if path.startswith(SOURCE_PREFIX):
            for hid in hid_devices:
                properties = dict(
                    line.split("=", 1)
                    for line in read_attribute(hid / "uevent").splitlines()
                    if "=" in line
                )
                matches = (
                    properties.get("HID_PHYS") == phys
                    if phys
                    else hid.name == Path(path).parent.name
                )
                if matches and expected_model:
                    try:
                        _, vendor, product = properties.get("HID_ID", "").split(":")
                        matches = (
                            f"{int(vendor, 16):04x}", f"{int(product, 16):04x}"
                        ) == expected_model
                    except ValueError:
                        matches = False
                if matches:
                    matched.update(
                        item.identity
                        for item in attachments
                        if hid.resolve().is_relative_to(item.syspath)
                    )
        # A model-only selector must never choose between identical receivers.
        if model and not phys and len(matched) != 1:
            continue
        identities.update(matched)
    return identities


class DeviceMaskingPanel(HardwareMaskingPanel):
    def __init__(
        self,
        parent=None,
        *,
        deferred: bool = False,
        remember: Callable[[str, Callable[[], None]], None] | None = None,
        details_group=None,
    ) -> None:
        super().__init__(
            parent,
            identities=set(),
            deferred=deferred,
            remember=remember,
            details_group=details_group,
        )
        self._resolution = 0
        self._resolving = False
        self._sources: tuple[tuple[str, str], ...] = ()
        self._saved: tuple[str, ...] = ()
        self._hardware_id = ""
        self._devices.set_description(None)
        self._devices.set_tooltip_text(
            "Hide the original device from apps while Keymasq remaps it. "
            "Shared receivers are masked together."
            + (" Applied after saving hardware." if deferred else "")
        )

    def set_sources(
        self,
        sources: Sequence[tuple[str, str]],
        saved: Sequence[str] = (),
        *,
        hardware_id: str = "",
    ) -> None:
        if (tuple(sources), tuple(saved), hardware_id) == (
            self._sources, self._saved, self._hardware_id
        ):
            return
        self._sources, self._saved = tuple(sources), tuple(saved)
        self._hardware_id = hardware_id
        self._resolution += 1
        self.identities = set(saved)
        self.choices.clear()
        for row in self._rows.values():
            row.remove_details()
            self._devices.remove(row)
        self._rows.clear()
        self._status.set_text("Finding physical device…")
        self._status.set_visible(True)
        self.footer.set_visible(True)
        self._resolve_targets()

    def _refresh(self) -> bool:
        if not self._closed:
            self._resolve_targets()
        return super()._refresh()

    def _resolve_targets(self) -> None:
        if self._resolving:
            return
        self._resolving = True
        resolution, sources, hardware_id = self._resolution, self._sources, self._hardware_id

        def resolved(result: GuiTaskResult[set[str]]) -> None:
            self._resolving = False
            if resolution != self._resolution or self._closed:
                return
            if result.error is not None:
                self._errors["all"] = "Could not identify the physical device. Reopen to retry."
            else:
                self._errors.pop("all", None)
                self.identities = (self.identities or set()) | (result.value or set())
            if self._state:
                self._render(self._state)

        run_gui_task(lambda: resolve_masking_devices(sources, hardware_id=hardware_id), resolved)
