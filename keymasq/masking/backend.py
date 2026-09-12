"""Bounded Linux device-policy transactions used only by the root mask service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from keymasq.common.types import JsonObject
from keymasq.masking.inventory import HID_NAME, USB_NAME, Attachment, HardwareInventory

log = logging.getLogger("keymasq.masking")
RUNTIME_DIR = Path("/run/keymasq-masking")
STATE_DIR = Path("/var/lib/keymasq-masking")
SOCKET_PATH = RUNTIME_DIR / "socket"


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
    ) -> None:
        self.inventory = inventory or HardwareInventory()
        self.runtime_dir = runtime_dir
        self.rules_dir = rules_dir
        self.state_dir = state_dir
        self.journal = runtime_dir / "journal.json"
        self.early = rules_dir / "72-keymasq-masking.rules"
        self.late = rules_dir / "99-zz-keymasq-masking.rules"
        self.mode_path = self.inventory.sys_root / "module/hid_steam/parameters/lizard_mode"

    def prepare_directories(self) -> None:
        for path in (self.runtime_dir, self.state_dir):
            path.mkdir(mode=0o755, parents=True, exist_ok=True)
        self.rules_dir.mkdir(parents=True, exist_ok=True)

    async def snapshot(self, attachment: Attachment) -> JsonObject:
        self.validate(attachment)
        if await finish_io(self.other_steam_controllers, attachment.main_hid):
            raise ValueError("Disconnect other hid-steam controllers before trying Deck masking")
        await finish_io(self.reject_unrevoked_handles, attachment)
        snapshots: dict[str, object] = {}
        for node in await finish_io(self.inventory.nodes, attachment):
            info = await finish_io(node.stat)
            if not stat.S_ISCHR(info.st_mode):
                raise ValueError("Hardware node changed during activation")
            role = await finish_io(self.inventory.node_role, attachment, node)
            snapshots[role] = {
                "uid": info.st_uid,
                "gid": info.st_gid,
                "acl": await run_host("getfacl", "-c", "--", str(node)),
            }
        original_mode = (await finish_io(self.mode_path.read_text)).strip()
        if original_mode not in {"Y", "N"}:
            raise ValueError("Unsupported hid-steam input mode")
        data: JsonObject = {
            "id": attachment.identity,
            "generation": attachment.generation,
            "main_hid": attachment.main_hid,
            "mode": original_mode,
            "nodes": snapshots,
        }
        await finish_io(save_json, self.journal, data)
        return data

    def other_steam_controllers(self, main_hid: str) -> list[str]:
        driver = self.inventory.sys_root / "bus/hid/drivers/hid-steam"
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
            str(node)
            for node in self.inventory.nodes(attachment)
            if "bus/usb" in str(node)
            or (
                node.name.startswith("hidraw")
                and not self.inventory.node_role(attachment, node).startswith(
                    f"{attachment.kernel_name}:1.2:"
                )
            )
        }
        for process in Path("/proc").iterdir():
            if not process.name.isdecimal():
                continue
            try:
                for descriptor in (process / "fd").iterdir():
                    try:
                        target = os.readlink(descriptor)
                    except FileNotFoundError:
                        continue
                    if target in protected:
                        raise ValueError(
                            f"Process {process.name} has a direct USB or auxiliary HID connection. "
                            "Close that application and retry masking."
                        )
            except (FileNotFoundError, ProcessLookupError):
                continue

    def validate(self, attachment: Attachment) -> None:
        if not attachment.supported or not USB_NAME.fullmatch(attachment.kernel_name):
            raise ValueError("This hardware does not have a supported takeover adapter")
        if not HID_NAME.fullmatch(attachment.main_hid):
            raise ValueError("Controller driver identity changed")

    def rule_matches(self, attachment: Attachment) -> list[str]:
        self.validate(attachment)
        name = attachment.kernel_name
        devnum = attachment.generation.split(":", 1)[0]
        if not devnum.isdecimal():
            raise ValueError("Invalid USB connection generation")
        parent = (
            f'KERNELS=="{name}", ATTRS{{idVendor}}=="28de", '
            f'ATTRS{{idProduct}}=="1205", ATTRS{{devnum}}=="{devnum}"'
        )
        return [
            f'SUBSYSTEM=="hidraw", {parent}',
            f'SUBSYSTEM=="usb", KERNEL=="{name}", ATTR{{idVendor}}=="28de", '
            f'ATTR{{idProduct}}=="1205", ATTR{{devnum}}=="{devnum}"',
            f'SUBSYSTEM=="input", KERNEL=="event*|js*", {parent}',
        ]

    async def activate(self, attachment: Attachment) -> list[str]:
        await self.snapshot(attachment)
        # Early tag removal must precede seat-late's uaccess processing. The
        # final rule removes existing ACLs and applies to newly created nodes.
        matches = self.rule_matches(attachment)
        early = "\n".join(f'ACTION=="add|change", {match}, TAG-="uaccess"' for match in matches)
        late_lines: list[str] = []
        setfacl = shutil.which("setfacl") or "/usr/bin/setfacl"
        for index, match in enumerate(matches):
            node = ("/dev/%k", "/dev/bus/usb/$env{BUSNUM}/$env{DEVNUM}", "/dev/input/%k")[index]
            late_lines.append(
                f'ACTION=="add|change", {match}, TAG-="uaccess", '
                'OWNER:="root", GROUP:="keymasq", MODE:="0660", '
                f'RUN+="{setfacl} -b {node}"'
            )
        await finish_io(self.early.write_text, early + "\n")
        await finish_io(self.late.write_text, "\n".join(late_lines) + "\n")
        await run_host("udevadm", "control", "--reload-rules")
        await self.trigger(attachment, "change")
        await self.rebind(attachment, attachment.main_hid)
        await finish_io(self.reject_unrevoked_handles, attachment)
        await finish_io(self.mode_path.write_text, "N\n")
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
        if "Steam Deck" not in names:
            raise OSError("hid-steam did not create physical controller input")
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

    async def trigger(self, attachment: Attachment, action: str) -> None:
        await run_host(
            "udevadm",
            "trigger",
            f"--action={action}",
            f"--parent-match={attachment.syspath}",
            "--settle",
            timeout=12.0,
        )

    async def rebind(self, attachment: Attachment, main_hid: str) -> None:
        if not HID_NAME.fullmatch(main_hid):
            raise ValueError("Invalid recorded HID identity")
        live = await finish_io(self.inventory.resolve, attachment.identity, attachment.generation)
        hid = self.inventory.sys_root / "bus/hid/devices" / main_hid
        if hid.resolve().parent != live.syspath / f"{live.kernel_name}:1.2":
            raise ValueError("Controller interface changed during takeover")
        driver = self.inventory.sys_root / "bus/hid/drivers/hid-steam"
        if (hid / "driver").is_symlink():
            if (hid / "driver").resolve() != driver.resolve():
                raise ValueError("Controller now uses a different driver")
            await finish_io((driver / "unbind").write_text, main_hid)
        await finish_io((driver / "bind").write_text, main_hid)
        await run_host("udevadm", "settle", "--timeout=8")

    async def recover(self) -> None:
        if not self.journal.exists():
            # Stale task-owned rules must never survive an interrupted startup.
            for rule in (self.early, self.late):
                await finish_io(rule.unlink, missing_ok=True)
            await run_host("udevadm", "control", "--reload-rules")
            return
        data = cast(JsonObject, json.loads(await finish_io(self.journal.read_text)))
        attachment: Attachment | None = None
        try:
            attachment = await finish_io(
                self.inventory.resolve, str(data["id"]), str(data["generation"])
            )
        except ValueError:
            log.info("Original attachment is gone; restoring policy without touching a replacement")
        if attachment is not None:
            await self.rebind(attachment, str(data["main_hid"]))
        mode = data.get("mode")
        if mode in {"Y", "N"} and self.mode_path.exists():
            await finish_io(self.mode_path.write_text, f"{mode}\n")
        for rule in (self.early, self.late):
            await finish_io(rule.unlink, missing_ok=True)
        await run_host("udevadm", "control", "--reload-rules")
        if attachment is not None:
            # Legacy evdev hiding must not undo recovery after a daemon crash.
            for node in await finish_io(self.inventory.nodes, attachment):
                if node.name.startswith(("event", "js")):
                    await finish_io(
                        (Path("/run/keymasq/hidden") / node.name).unlink, missing_ok=True
                    )
            await finish_io(Path("/run/keymasq/hidden-hardware/28de:1205").unlink, missing_ok=True)
            await self.trigger(attachment, "add")
            snapshots = cast(dict[str, JsonObject], data["nodes"])
            for node in await finish_io(self.inventory.nodes, attachment):
                role = await finish_io(self.inventory.node_role, attachment, node)
                snapshot = snapshots.get(role)
                if snapshot is None:
                    continue
                await finish_io(
                    os.chown, node, int(str(snapshot["uid"])), int(str(snapshot["gid"]))
                )
                acl_file = self.runtime_dir / "restore.acl"
                await finish_io(acl_file.write_text, str(snapshot["acl"]))
                await run_host("setfacl", f"--set-file={acl_file}", "--", str(node))
            await finish_io((self.runtime_dir / "restore.acl").unlink, missing_ok=True)
        await finish_io(self.journal.unlink)
