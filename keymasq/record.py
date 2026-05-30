import argparse
import json
import os
import pwd
import sys
import time
from pathlib import Path

from keymasq.common.recording_guard import (
    resolve_unlock_status,
    runtime_unlock_path,
    write_unlock_expires_at,
)


def _require_privileged_caller() -> None:
    uid = os.geteuid()
    if uid == 0:
        return

    try:
        keymasq_uid = pwd.getpwnam("keymasq").pw_uid
    except KeyError:
        keymasq_uid = -1

    if uid != keymasq_uid:
        raise PermissionError("keymasq-record must run as root or keymasq user")


def _write_lease(path: Path, expires_at: int) -> None:
    owner_uid = None
    owner_gid = None
    try:
        keymasq_user = pwd.getpwnam("keymasq")
        owner_uid = keymasq_user.pw_uid
        owner_gid = keymasq_user.pw_gid
    except Exception:
        pass

    write_unlock_expires_at(
        path,
        expires_at,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        mode=0o644,
    )


def _remove_lease(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(prog="keymasq-record")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="Show unlock status")
    status_parser.add_argument("--uid", type=int, required=True)

    runtime_unlock_parser = subparsers.add_parser("unlock-runtime", help="Set runtime unlock")
    runtime_unlock_parser.add_argument("--uid", type=int, required=True)
    runtime_unlock_parser.add_argument("--ttl", type=int, default=900)

    runtime_lock_parser = subparsers.add_parser("lock-runtime", help="Clear runtime unlock")
    runtime_lock_parser.add_argument("--uid", type=int, required=True)

    args = parser.parse_args()

    try:
        _require_privileged_caller()

        if args.command == "status":
            status = resolve_unlock_status(args.uid)
            print(json.dumps({"status": "ok", **status}))
            return

        if args.command == "unlock-runtime":
            runtime_path = runtime_unlock_path(args.uid)
            if int(args.ttl) == 0:
                expires_at = 0
            else:
                expires_at = int(time.time()) + max(1, int(args.ttl))
            _write_lease(runtime_path, expires_at)
            print(json.dumps({"status": "ok", "scope": "runtime", "expires_at": expires_at}))
            return

        if args.command == "lock-runtime":
            _remove_lease(runtime_unlock_path(args.uid))
            print(json.dumps({"status": "ok", "scope": "runtime", "locked": True}))
            return

        raise ValueError("Unknown command")
    except Exception as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
