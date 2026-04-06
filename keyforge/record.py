import argparse
import json
import os
import pwd
import sys
import time
from pathlib import Path

from keyforge.common.recording_guard import (
    parse_unlock_expires_at,
    persistent_unlock_path,
    resolve_unlock_status,
    runtime_unlock_path,
)


def _require_privileged_caller() -> None:
    uid = os.geteuid()
    if uid == 0:
        return

    try:
        keyforge_uid = pwd.getpwnam("keyforge").pw_uid
    except KeyError:
        keyforge_uid = -1

    if uid != keyforge_uid:
        raise PermissionError("keyforge-record must run as root or keyforge user")


def _write_lease(path: Path, expires_at: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{int(expires_at)}\n", encoding="utf-8")

    try:
        keyforge_user = pwd.getpwnam("keyforge")
        os.chown(path, keyforge_user.pw_uid, keyforge_user.pw_gid)
    except Exception:
        pass

    os.chmod(path, 0o644)


def _remove_lease(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(prog="keyforge-record")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="Show unlock status")
    status_parser.add_argument("--uid", type=int, required=True)

    runtime_unlock_parser = subparsers.add_parser("unlock-runtime", help="Set runtime unlock")
    runtime_unlock_parser.add_argument("--uid", type=int, required=True)
    runtime_unlock_parser.add_argument("--ttl", type=int, default=900)

    runtime_lock_parser = subparsers.add_parser("lock-runtime", help="Clear runtime unlock")
    runtime_lock_parser.add_argument("--uid", type=int, required=True)

    persistent_unlock_parser = subparsers.add_parser(
        "unlock-persistent", help="Set persistent unlock"
    )
    persistent_unlock_parser.add_argument("--uid", type=int, required=True)
    persistent_unlock_parser.add_argument("--ttl", type=int, default=0)

    persistent_lock_parser = subparsers.add_parser(
        "lock-persistent", help="Clear persistent unlock"
    )
    persistent_lock_parser.add_argument("--uid", type=int, required=True)

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
                candidate = int(time.time()) + max(1, int(args.ttl))
                previous = parse_unlock_expires_at(runtime_path)
                if previous is not None:
                    candidate = max(candidate, int(previous) + 1)
                expires_at = candidate
            _write_lease(runtime_path, expires_at)
            print(json.dumps({"status": "ok", "scope": "runtime", "expires_at": expires_at}))
            return

        if args.command == "lock-runtime":
            _remove_lease(runtime_unlock_path(args.uid))
            print(json.dumps({"status": "ok", "scope": "runtime", "locked": True}))
            return

        if args.command == "unlock-persistent":
            expires_at = 0 if int(args.ttl) == 0 else int(time.time()) + max(1, int(args.ttl))
            _write_lease(persistent_unlock_path(args.uid), expires_at)
            print(json.dumps({"status": "ok", "scope": "persistent", "expires_at": expires_at}))
            return

        if args.command == "lock-persistent":
            _remove_lease(persistent_unlock_path(args.uid))
            print(json.dumps({"status": "ok", "scope": "persistent", "locked": True}))
            return

        raise ValueError("Unknown command")
    except Exception as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
