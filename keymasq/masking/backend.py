"""Bounded Linux device-policy transactions used only by short-lived root helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
import re
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from keymasq.common.types import JsonObject
from keymasq.masking.inventory import (
    DRIVER_NAME,
    HID_NAME,
    USB_INTERFACE_NAME,
    USB_NAME,
    Attachment,
    HardwareInventory,
)

log = logging.getLogger("keymasq.masking")
RUNTIME_DIR = Path("/run/keymasq-masking")
STATE_DIR = Path("/var/lib/keymasq-masking")


class DeviceInUseError(ValueError):
    def __init__(self, pid: str, application: str) -> None:
        self.pid = pid
        self.application = application
        super().__init__(
            f"Process {pid} has a direct USB or auxiliary HID connection. "
            "Close that application and retry masking."
        )


async def finish_io[T](function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Wait for an in-flight filesystem mutation before allowing rollback."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def run_host(*args: str, timeout: float = 8.0) -> str:
    process = await asyncio.create_subprocess_exec(
        shutil.which(args[0]) or args[0],
        *args[1:],
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin:/run/current-system/sw/bin", "LC_ALL": "C"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise OSError(f"{args[0]} failed: {stderr.decode(errors='replace').strip()}")
    return stdout.decode(errors="replace")


def save_json(path: Path, data: JsonObject) -> None:
    temporary = path.with_suffix(".new")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class LinuxMaskBackend:
    def __init__(
        self,
        inventory: HardwareInventory | None = None,
        runtime_dir: Path = RUNTIME_DIR,
        rules_dir: Path = Path("/run/udev/rules.d"),
        state_dir: Path = STATE_DIR,
        reservation_id: str = "",
    ) -> None:
        self.inventory = inventory or HardwareInventory()
        self.runtime_dir = runtime_dir
        self.rules_dir = rules_dir
        self.state_dir = state_dir
        self.journal = runtime_dir / "journal.json"
        self.armed_record = runtime_dir / "armed.json"
        self.permissions = runtime_dir / "permissions.json"
        suffix = f"-{reservation_id}" if reservation_id else ""
        self.early = rules_dir / f"72-keymasq-masking{suffix}.rules"
        self.late = rules_dir / f"99-zz-keymasq-masking{suffix}.rules"
        self.mode_path = self.inventory.sys_root / "module/hid_steam/parameters/lizard_mode"
        self.active_attachment: Attachment | None = None

    @property
    def armed(self) -> bool:
        return self.early.exists() or self.late.exists() or self.armed_record.exists()

    def for_attachment(self, identity: str) -> LinuxMaskBackend:
        if not re.fullmatch(r"[0-9a-f]{24}", identity):
            raise ValueError("Invalid attachment identity")
        return LinuxMaskBackend(
            self.inventory,
            self.runtime_dir / "reservations" / identity,
            self.rules_dir,
            self.state_dir / "reservations" / identity,
            reservation_id=identity,
        )

    def reservation_ids(self) -> list[str]:
        return sorted(
            {
                path.name
                for root in (self.runtime_dir, self.state_dir)
                for path in (root / "reservations").glob("*")
                if re.fullmatch(r"[0-9a-f]{24}", path.name) and path.is_dir()
            }
        )

    def prepare_directories(self) -> None:
        for path in (self.runtime_dir, self.state_dir):
            path.mkdir(mode=0o755, parents=True, exist_ok=True)
        self.rules_dir.mkdir(parents=True, exist_ok=True)

    async def snapshot(self, attachment: Attachment) -> JsonObject:
        await finish_io(self.validate, attachment)
        await self.check_deck_scope(attachment)
        try:
            bindings = await finish_io(self.inventory.bindings, attachment)
        except ValueError as exc:
            if (
                attachment.transport != "usb"
                or str(exc) != "No bound input interfaces are available to reconnect"
            ):
                raise
            bindings = {}  # A direct USB client may have detached the kernel driver.
        for binding, driver_name in bindings.items():
            bus = "usb" if binding.startswith("usb:") else "hid"
            driver = self.inventory.sys_root / f"bus/{bus}/drivers" / driver_name
            if not await finish_io((driver / "bind").exists) or not await finish_io(
                (driver / "unbind").exists
            ):
                raise ValueError(f"Input driver {driver_name} does not support rebinding")
        if attachment.transport != "usb":
            await finish_io(self.reject_unrevoked_handles, attachment)
        snapshots: dict[str, object] = {}
        for node in await finish_io(self.inventory.nodes, attachment):
            info = await finish_io(node.stat)
            if not stat.S_ISCHR(info.st_mode):
                raise ValueError("Hardware node changed during activation")
            role = await finish_io(self.inventory.node_role, attachment, node)
            if role in snapshots:
                raise ValueError(f"Ambiguous hardware interface role: {role}")
            snapshots[role] = {
                "uid": info.st_uid,
                "gid": info.st_gid,
                "acl": await run_host("getfacl", "-c", "--", str(node)),
                "inode": info.st_ino,
                "filesystem": info.st_dev,
            }
        original_mode = ""
        if attachment.is_deck and await finish_io(self.mode_path.exists):
            original_mode = (await finish_io(self.mode_path.read_text)).strip()
            if original_mode not in {"Y", "N"}:
                raise ValueError("Unsupported hid-steam input mode")
        data: JsonObject = {
            "id": attachment.identity,
            "generation": attachment.generation,
            "mode": original_mode,
            "nodes": snapshots,
            "bindings": bindings,
            "prearmed": self.armed,
            "selector": self.selector(attachment),
        }
        await finish_io(save_json, self.journal, data)
        return data

    async def check_deck_scope(self, attachment: Attachment) -> None:
        if attachment.is_deck and await finish_io(
            self.other_steam_controllers, attachment.main_hid, attachment_path=attachment.syspath
        ):
            raise ValueError("Disconnect other hid-steam controllers before trying Deck masking")

    def other_steam_controllers(
        self, main_hid: str, *, attachment_path: Path | None = None
    ) -> list[str]:
        if not main_hid and attachment_path is None:
            return []
        driver = self.inventory.sys_root / "bus/hid/drivers/hid-steam"
        if attachment_path is None:
            attachment_path = (
                (self.inventory.sys_root / "bus/hid/devices" / main_hid).resolve().parent.parent
            )
        return [
            path.name
            for path in driver.glob("*")
            if HID_NAME.fullmatch(path.name) and not path.resolve().is_relative_to(attachment_path)
        ]

    def reject_unrevoked_handles(self, attachment: Attachment) -> None:
        protected = {
            node.stat().st_rdev
            for node in self.inventory.nodes(attachment)
            if "bus/usb" in str(node)
            or (
                attachment.is_deck
                and node.name.startswith("hidraw")
                and not self.inventory.node_role(attachment, node).startswith(
                    f"{attachment.kernel_name}:1.2:"
                )
            )
        }
        if not protected:
            return
        for process in Path("/proc").iterdir():
            if not process.name.isdecimal():
                continue
            try:
                for descriptor in (process / "fd").iterdir():
                    try:
                        info = descriptor.stat()
                    except FileNotFoundError:
                        continue
                    if stat.S_ISCHR(info.st_mode) and info.st_rdev in protected:
                        try:
                            application = (
                                (process / "comm").read_text().strip().splitlines()[0][:80]
                            )
                        except (OSError, IndexError):
                            application = ""
                        raise DeviceInUseError(process.name, application)
            except (FileNotFoundError, ProcessLookupError):
                continue

    def validate(self, attachment: Attachment) -> None:
        pattern = USB_NAME if attachment.transport == "usb" else HID_NAME
        if not attachment.supported or not pattern.fullmatch(attachment.kernel_name):
            raise ValueError("Invalid physical attachment")
        if attachment.transport == "usb" and self.inventory.is_hub(attachment.syspath):
            raise ValueError("Masking USB hubs is not supported")
        if not all(
            len(value) == 4 and all(char in "0123456789abcdef" for char in value)
            for value in (attachment.vendor, attachment.product)
        ):
            raise ValueError("Invalid hardware vendor or product")

    def rule_matches(self, attachment: Attachment) -> list[str]:
        self.validate(attachment)
        name = attachment.kernel_name
        if attachment.transport != "usb":
            return [
                f'SUBSYSTEM=="hidraw", KERNELS=="{name}"',
                f'SUBSYSTEM=="input", KERNEL=="event*|js*", KERNELS=="{name}"',
            ]
        # Match the selected physical device across USB connection generations.
        # A serial is matched when present so replacing it with the same model
        # does not inherit the desktop access restriction.
        serial = self.rule_literal(attachment.serial)
        device_path = self.rule_literal(
            "/" + str(attachment.syspath.relative_to(self.inventory.sys_root))
        )
        child_path = device_path[:-1] + '/*"'
        parent = (
            f"DEVPATH=={child_path}, "
            f'KERNELS=="{name}", ATTRS{{idVendor}}=="{attachment.vendor}", '
            f'ATTRS{{idProduct}}=="{attachment.product}"'
        )
        if attachment.serial:
            parent += f", ATTRS{{serial}}=={serial}"
        else:
            # Missing sysfs attributes fail even a negative ATTR match. Test
            # absence at the selected USB parent, including for child nodes.
            parent += f", TEST!={self.rule_literal(str(attachment.syspath / 'serial'))}"
        device = (
            f'SUBSYSTEM=="usb", DEVPATH=={device_path}, KERNEL=="{name}", '
            f'ATTR{{idVendor}}=="{attachment.vendor}", '
            f'ATTR{{idProduct}}=="{attachment.product}"'
        )
        if attachment.serial:
            device += f", ATTR{{serial}}=={serial}"
        else:
            device += f", TEST!={self.rule_literal(str(attachment.syspath / 'serial'))}"
        return [
            f'SUBSYSTEM=="hidraw", {parent}',
            device,
            f'SUBSYSTEM=="input", KERNEL=="event*|js*", {parent}',
        ]

    @staticmethod
    def rule_literal(value: str) -> str:
        # udev treats | as an alternative even inside a bracket expression.
        if any(character in value for character in ("|", "\0", "\\")):
            raise ValueError("The USB serial cannot be matched safely by udev")
        pattern = "".join({"*": "[*]", "?": "[?]", "[": "[[]", "]": "[]]"}.get(c, c) for c in value)
        return 'e"' + "".join(f"\\x{byte:02x}" for byte in pattern.encode()) + '"'

    @staticmethod
    def selector(attachment: Attachment) -> JsonObject:
        return {
            "id": attachment.identity,
            "vendor": attachment.vendor,
            "product": attachment.product,
            "transport": attachment.transport,
            "path": str(attachment.syspath),
            "kernel_name": attachment.kernel_name,
            "serial": attachment.serial,
        }

    def from_selector(self, selector: JsonObject) -> Attachment:
        attachment = Attachment(
            str(selector["id"]),
            "",
            "",
            str(selector["vendor"]),
            str(selector["product"]),
            str(selector["transport"]),
            Path(str(selector["path"])),
            str(selector["kernel_name"]),
            serial=str(selector.get("serial", "")),
        )
        self.validate(attachment)
        if not attachment.syspath.is_relative_to(self.inventory.sys_root / "devices"):
            raise ValueError("Invalid saved USB attachment path")
        return attachment

    async def install_rules(self, attachment: Attachment) -> None:
        from keymasq.masking.permissions import capture

        # Early tag removal must precede seat-late's uaccess processing. The
        # final rule removes existing ACLs and applies to newly created nodes.
        matches = await finish_io(self.rule_matches, attachment)
        if not self.armed:
            for live in await finish_io(self.inventory.scan):
                if live.identity == attachment.identity:
                    await capture(self, live)
                    break
        await finish_io(save_json, self.armed_record, self.selector(attachment))
        # Driver bind/unbind events also run desktop access rules. Restrict
        # every event for a live node, not only its initial add/change.
        early = "\n".join(f'ACTION!="remove", {match}, TAG-="uaccess"' for match in matches)
        late_lines: list[str] = []
        setfacl = shutil.which("setfacl") or "/usr/bin/setfacl"
        chmod = shutil.which("chmod") or "/usr/bin/chmod"
        for match in matches:
            node = (
                "/dev/bus/usb/$env{BUSNUM}/$env{DEVNUM}"
                if match.startswith('SUBSYSTEM=="usb"')
                else "/dev/input/%k"
                if match.startswith('SUBSYSTEM=="input"')
                else "/dev/%k"
            )
            late_lines.append(
                f'ACTION!="remove", {match}, TAG-="uaccess", '
                'OWNER:="root", GROUP:="keymasq", MODE:="0660", '
                f'RUN+="{chmod} 0660 {node}", '
                f'RUN+="{setfacl} -b {node}"'
            )

        def write_rule(path: Path, content: str) -> None:
            temporary = path.with_suffix(".new")
            temporary.write_text(content)
            temporary.chmod(0o644)
            temporary.replace(path)

        await finish_io(write_rule, self.late, "\n".join(late_lines) + "\n")
        await finish_io(write_rule, self.early, early + "\n")
        await run_host("udevadm", "control", "--reload-rules")

    async def activate(self, attachment: Attachment) -> list[str]:
        from keymasq.masking.usb import reconnect

        self.active_attachment = None
        snapshot = await self.snapshot(attachment)
        await self.install_rules(attachment)
        await self.trigger(attachment, "change")
        try:
            await finish_io(self.reject_unrevoked_handles, attachment)
        except DeviceInUseError:
            if attachment.transport != "usb":
                raise
            attachment = await reconnect(self, attachment, snapshot)
        else:
            if attachment.transport == "usb" and not snapshot["bindings"]:
                attachment = await reconnect(self, attachment, snapshot)
            else:
                for hid, driver in cast(dict[str, str], snapshot["bindings"]).items():
                    await self.rebind_binding(attachment, hid, driver)
        await finish_io(self.reject_unrevoked_handles, attachment)
        if attachment.is_deck:
            await self.check_deck_scope(attachment)
            if snapshot.get("mode") not in {"Y", "N"}:
                mode = (await finish_io(self.mode_path.read_text)).strip()
                if mode not in {"Y", "N"}:
                    raise ValueError("Unsupported hid-steam input mode")
                snapshot["mode"] = mode
                await finish_io(save_json, self.journal, snapshot)
            await finish_io(self.mode_path.write_text, "N\n")
        current = await finish_io(
            self.inventory.resolve, attachment.identity, attachment.generation
        )
        expected = set(cast(dict[str, object], snapshot["nodes"]))
        deadline = asyncio.get_running_loop().time() + 8
        while not expected <= set(await finish_io(self.inventory.endpoint_roles, current)):
            if asyncio.get_running_loop().time() >= deadline:
                raise OSError("Some reserved hardware interfaces did not return after reconnect")
            await asyncio.sleep(0.1)
            current = await finish_io(
                self.inventory.resolve, attachment.identity, attachment.generation
            )
        nodes = await finish_io(self.inventory.event_nodes, current)
        await self.verify_access(current)
        names = [
            (
                await finish_io(
                    (
                        self.inventory.sys_root / "class/input" / Path(node).name / "device/name"
                    ).read_text
                )
            ).strip()
            for node in nodes
        ]
        if attachment.is_deck and "Steam Deck" not in names:
            raise OSError("hid-steam did not create physical controller input")
        roles = {
            await finish_io(self.inventory.node_role, current, node)
            for node in await finish_io(self.inventory.nodes, current)
        }
        if not set(cast(dict[str, object], snapshot["nodes"])) <= roles:
            raise OSError("Some reserved hardware interfaces did not return after reconnect")
        for node in await finish_io(self.inventory.nodes, current):
            if not node.name.startswith(("hidraw", "event", "js")):
                continue
            role = await finish_io(self.inventory.node_role, current, node)
            if attachment.is_deck and not role.startswith(f"{attachment.kernel_name}:1.2:"):
                continue
            before = cast(dict[str, JsonObject], snapshot["nodes"]).get(role)
            info = await finish_io(node.stat)
            if before and (before.get("filesystem"), before.get("inode")) == (
                info.st_dev,
                info.st_ino,
            ):
                raise OSError(f"Input endpoint {node.name} was not replaced during takeover")
        self.active_attachment = current
        return nodes

    async def verify_access(self, attachment: Attachment) -> None:
        expected_gid = pwd.getpwnam("keymasq").pw_gid
        for node in await finish_io(self.inventory.nodes, attachment):
            info = await finish_io(node.stat)
            if (
                info.st_uid != 0
                or info.st_gid != expected_gid
                or stat.S_IMODE(info.st_mode) != 0o660
            ):
                raise OSError(f"Access policy did not reserve {node.name}")
            acl = await run_host("getfacl", "-c", "--", str(node))
            if any(
                line.startswith(("user:", "group:")) and line.split(":")[1]
                for line in acl.splitlines()
            ):
                raise OSError(f"A desktop ACL still permits access to {node.name}")

    async def refresh_interfaces(self, attachment: Attachment) -> list[str]:
        """Adopt hotplugged input nodes without resetting an already masked receiver."""
        if not self.armed or not self.journal.exists():
            raise ValueError("The hardware mask is no longer active")
        await run_host("udevadm", "settle", "--timeout=8")
        current = await finish_io(
            self.inventory.resolve, attachment.identity, attachment.generation
        )
        await self.verify_access(current)
        await finish_io(self.reject_unrevoked_handles, current)
        return await finish_io(self.inventory.event_nodes, current)

    async def trigger(self, attachment: Attachment, action: str) -> None:
        await run_host(
            "udevadm",
            "trigger",
            f"--action={action}",
            f"--parent-match={attachment.syspath}",
            "--settle",
            timeout=12.0,
        )

    async def rebind_hid(
        self, attachment: Attachment, main_hid: str, driver_name: str, *, repair: bool = False
    ) -> None:
        if not HID_NAME.fullmatch(main_hid):
            raise ValueError("Invalid recorded HID identity")
        if not DRIVER_NAME.fullmatch(driver_name):
            raise ValueError("Invalid recorded HID driver")
        live = await finish_io(self.inventory.resolve, attachment.identity, attachment.generation)
        hid = self.inventory.sys_root / "bus/hid/devices" / main_hid
        if not hid.exists() or not hid.resolve().is_relative_to(live.syspath):
            raise ValueError("Controller interface changed during takeover")
        driver = self.inventory.sys_root / "bus/hid/drivers" / driver_name
        if (hid / "driver").is_symlink():
            if (hid / "driver").resolve() != driver.resolve():
                raise ValueError("Controller now uses a different driver")
            if repair:
                return
            await finish_io((driver / "unbind").write_text, main_hid)
            # A successful sysfs write alone is insufficient. These children
            # must actually disappear before replacement nodes can be published.
            children = await finish_io(lambda: list(hid.glob("hidraw/hidraw*")))
            if children:
                raise OSError("HID driver did not remove its raw endpoints")
        await finish_io((driver / "bind").write_text, main_hid)
        await run_host("udevadm", "settle", "--timeout=8")

    async def rebind_binding(
        self, attachment: Attachment, binding: str, driver_name: str, *, repair: bool = False
    ) -> None:
        if not binding.startswith("usb:"):
            await self.rebind_hid(attachment, binding, driver_name, repair=repair)
            return
        name = binding.removeprefix("usb:")
        if not USB_INTERFACE_NAME.fullmatch(name) or not DRIVER_NAME.fullmatch(driver_name):
            raise ValueError("Invalid recorded USB input binding")
        live = await finish_io(self.inventory.resolve, attachment.identity, attachment.generation)
        interface = self.inventory.sys_root / "bus/usb/devices" / name
        if not interface.exists() or interface.resolve().parent != live.syspath:
            raise ValueError("USB input interface changed during takeover")
        driver = self.inventory.sys_root / "bus/usb/drivers" / driver_name
        if (interface / "driver").is_symlink():
            if (interface / "driver").resolve() != driver.resolve():
                raise ValueError("USB input interface now uses a different driver")
            if repair:
                return
            await finish_io((driver / "unbind").write_text, name)
            if await finish_io(lambda: list(interface.glob("input/input*"))):
                raise OSError("USB input driver did not remove its endpoints")
        await finish_io((driver / "bind").write_text, name)
        await run_host("udevadm", "settle", "--timeout=8")

    async def recover(self, *, keep_rules: bool = False) -> None:
        from keymasq.masking.permissions import restore
        from keymasq.masking.usb import enable_recorded_port

        if not self.journal.exists():
            if keep_rules:
                return
            selector = (
                json.loads(await finish_io(self.armed_record.read_text))
                if self.armed_record.exists()
                else json.loads(await finish_io(self.permissions.read_text))["selector"]
                if self.permissions.exists()
                else {}
            )
            if not selector:
                await self.remove_rules()
                return
            # Persist recovery intent before removing rules, including when
            # startup armed a selector but never began an activation journal.
            await finish_io(
                save_json,
                self.journal,
                {
                    "id": selector["id"],
                    "generation": "",
                    "selector": selector,
                    "nodes": {},
                    "bindings": {},
                    "mode": "",
                    "prearmed": True,
                },
            )
        data = cast(JsonObject, json.loads(await finish_io(self.journal.read_text)))
        await finish_io(enable_recorded_port, self, data)
        attachment: Attachment | None = None
        repair_error: Exception | None = None
        try:
            attachment = await finish_io(
                self.inventory.resolve, str(data["id"]), str(data["generation"])
            )
        except ValueError:
            log.info("Original attachment is gone; restoring policy without touching a replacement")
            if data.get("selector", {}).get("transport") == "usb":
                attachment = next(
                    (
                        item
                        for item in await finish_io(self.inventory.scan)
                        if item.identity == data["id"]
                    ),
                    None,
                )
                if attachment is not None:
                    # Never apply the old generation's bindings to a replacement.
                    # Missing drivers must not skip independent access restoration.
                    data["bindings"] = {}
                    if data.get("usb_reconnect"):
                        try:
                            data["bindings"] = await finish_io(self.inventory.bindings, attachment)
                        except (OSError, ValueError) as exc:
                            repair_error = exc
                    data["prearmed"] = True
        if attachment is not None:
            for hid, driver in cast(dict[str, str], data["bindings"]).items():
                try:
                    await self.rebind_binding(
                        attachment, hid, driver, repair=data.get("mode") not in {"Y", "N"}
                    )
                except (OSError, ValueError) as exc:
                    repair_error = exc
        mode = data.get("mode")
        if mode in {"Y", "N"} and self.mode_path.exists():
            try:
                await finish_io(self.mode_path.write_text, f"{mode}\n")
            except OSError as exc:
                repair_error = exc
        if not keep_rules:
            await self.remove_rules()
        if attachment is not None and not keep_rules:
            # Legacy evdev hiding must not undo recovery after a daemon crash.
            for node in await finish_io(self.inventory.nodes, attachment):
                if node.name.startswith(("event", "js")):
                    await finish_io(
                        (Path("/run/keymasq/hidden") / node.name).unlink, missing_ok=True
                    )
            await finish_io(
                (
                    Path("/run/keymasq/hidden-hardware")
                    / f"{attachment.vendor}:{attachment.product}"
                ).unlink,
                missing_ok=True,
            )
            await restore(self, attachment, data)
            # Current rules and logind own the final desktop grants. No saved
            # ACL may overwrite their result, including after a seat switch.
            await self.trigger(attachment, "add")
        if repair_error is not None:
            raise repair_error
        await finish_io(self.journal.unlink)
        if not keep_rules:
            await finish_io(self.permissions.unlink, missing_ok=True)

    async def remove_rules(self) -> None:
        for rule in (self.early, self.late):
            await finish_io(rule.unlink, missing_ok=True)
        await run_host("udevadm", "control", "--reload-rules")
        await run_host("udevadm", "settle", "--timeout=8")
        await finish_io(self.armed_record.unlink, missing_ok=True)
