"""Exercise the FUSE handle exception against a real private user mount."""

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path


def serve(mountpoint: str) -> None:
    from fuse import FUSE, FuseOSError, Operations

    class Files(Operations):
        def getattr(self, path, fh=None):
            if path not in ("/", "/document"):
                raise FuseOSError(2)
            return {
                "st_mode": (stat.S_IFDIR | 0o755) if path == "/" else (stat.S_IFREG | 0o444),
                "st_nlink": 2 if path == "/" else 1,
                "st_size": 1,
                "st_uid": os.getuid(),
                "st_gid": os.getgid(),
            }

        def readdir(self, path, fh):
            return [".", "..", "document"]

        def open(self, path, flags):
            return 0

        def read(self, path, size, offset, fh):
            return b"x"[offset : offset + size]

    FUSE(Files(), mountpoint, foreground=True, nothreads=True, nodev=True)


def hold(mountpoint: str, ready: str) -> None:
    with open(Path(mountpoint) / "document", "rb") as stream:
        temporary = Path(ready).with_suffix(".new")
        temporary.write_text(json.dumps({"pid": os.getpid(), "fd": stream.fileno()}))
        temporary.replace(ready)
        # The controlling test owns the deadline and terminates this process.
        sys.stdin.read()


def check(user: str, mountpoint: str) -> None:
    from keymasq.masking.backend import LinuxMaskBackend

    root = Path(mountpoint)
    ready = root.parent / "holder.json"
    # A separate mount namespace exercises the inspected process's mount table.
    holder = subprocess.Popen(
        [
            "unshare",
            "--mount",
            "--propagation",
            "private",
            "runuser",
            "-u",
            user,
            "--",
            sys.executable,
            __file__,
            "hold",
            mountpoint,
            str(ready),
        ],
        stdin=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if holder.poll() is not None or time.monotonic() >= deadline:
                raise AssertionError("FUSE descriptor holder did not start")
            time.sleep(0.05)
        record = json.loads(ready.read_text())
        process, descriptor = Path(f"/proc/{record['pid']}"), str(record["fd"])
        target = process / "fd" / descriptor
        try:
            target.stat()
        except PermissionError:
            pass
        else:
            raise AssertionError("the private user FUSE mount did not deny root stat")
        assert LinuxMaskBackend._descriptor_cannot_be_a_device(process, descriptor)
        # After a lazy unmount in the holder's namespace, fdinfo still names
        # the mount but mountinfo cannot establish nodev. Refuse the exception.
        subprocess.run(
            ["nsenter", "--target", str(record["pid"]), "--mount", "umount", "-l", mountpoint],
            check=True,
            timeout=10,
        )
        assert not LinuxMaskBackend._descriptor_cannot_be_a_device(process, descriptor)
        print("FUSE scan: private nodev mount accepted; detached mount refused", flush=True)
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        try:
            holder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait(timeout=5)


if __name__ == "__main__":
    if sys.argv[1] == "serve":
        serve(sys.argv[2])
    elif sys.argv[1] == "hold":
        hold(sys.argv[2], sys.argv[3])
    else:
        check(sys.argv[2], sys.argv[3])
