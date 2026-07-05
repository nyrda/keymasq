import argparse
import json
import logging
import math
import os
import pwd
import sys
import time
from pathlib import Path

from keymasq.common.recording_guard import (
    persistent_macro_recording_path,
    resolve_macro_recording_status,
    resolve_unlock_status,
    runtime_macro_recording_path,
    runtime_unlock_path,
    write_unlock_expires_at,
)

log = logging.getLogger("keymasq.record")


def _keymasq_uid() -> int | None:
    try:
        return int(pwd.getpwnam("keymasq").pw_uid)
    except KeyError:
        return None


def _exit_error(message: object) -> None:
    print(json.dumps({"status": "error", "message": str(message)}))
    sys.exit(1)


def _require_privileged_caller() -> int:
    uid = os.geteuid()
    if uid == 0:
        return uid

    keymasq_uid = _keymasq_uid()

    if uid != keymasq_uid:
        raise PermissionError("keymasq-record must run as root or keymasq user")
    return uid


def _authorize_target_uid(target_uid: int, caller_euid: int) -> None:
    pkexec_uid = os.environ.get("PKEXEC_UID", "").strip()
    keymasq_uid = _keymasq_uid()
    trusted_pkexec_euid = caller_euid == 0 or (
        keymasq_uid is not None and int(caller_euid) == keymasq_uid
    )
    if pkexec_uid and trusted_pkexec_euid:
        try:
            authorized_uid = int(pkexec_uid)
        except ValueError as exc:
            raise PermissionError("Invalid PKEXEC_UID from authorization environment") from exc
        if int(target_uid) != authorized_uid:
            raise PermissionError("Target uid does not match authorized user")
        return

    if keymasq_uid is not None and int(caller_euid) == keymasq_uid:
        return

    if caller_euid != 0 and int(target_uid) != int(caller_euid):
        raise PermissionError("Target uid does not match caller uid")


def _runtime_expires_at(ttl: int) -> int:
    ttl = int(ttl)
    if ttl < 1:
        raise ValueError("runtime ttl must be at least 1 second")
    return math.ceil(time.time() + ttl)


def _write_lease(path: Path, expires_at: int, target_uid: int) -> None:
    owner_uid = os.geteuid()
    owner_gid = os.getegid()
    target_gid = owner_gid
    try:
        keymasq_user = pwd.getpwnam("keymasq")
        owner_uid = keymasq_user.pw_uid
        owner_gid = keymasq_user.pw_gid
    except KeyError:
        if owner_uid == 0:
            owner_gid = 0

    try:
        target_gid = pwd.getpwuid(int(target_uid)).pw_gid
    except KeyError:
        target_gid = owner_gid

    write_unlock_expires_at(
        path,
        expires_at,
        owner_uid=owner_uid,
        owner_gid=target_gid,
        mode=0o440,
    )


def _remove_lease(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(prog="keymasq-record")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="Show capture unlock status")
    status_parser.add_argument("--uid", type=int, required=True)

    runtime_unlock_parser = subparsers.add_parser(
        "unlock-runtime",
        help="Set runtime capture unlock",
    )
    runtime_unlock_parser.add_argument("--uid", type=int, required=True)
    runtime_unlock_parser.add_argument("--ttl", type=int, default=900)

    runtime_lock_parser = subparsers.add_parser(
        "lock-runtime",
        help="Clear runtime capture unlock",
    )
    runtime_lock_parser.add_argument("--uid", type=int, required=True)

    macro_recording_status_parser = subparsers.add_parser(
        "macro-recording-status",
        help="Show macro recording opt-in status",
    )
    macro_recording_status_parser.add_argument("--uid", type=int, required=True)

    macro_recording_runtime_parser = subparsers.add_parser(
        "enable-macro-recording-runtime",
        help="Enable macro recording until the runtime lease expires",
    )
    macro_recording_runtime_parser.add_argument("--uid", type=int, required=True)
    macro_recording_runtime_parser.add_argument("--ttl", type=int, default=3600)

    macro_recording_persistent_parser = subparsers.add_parser(
        "enable-macro-recording-persistent",
        help="Enable macro recording persistently",
    )
    macro_recording_persistent_parser.add_argument("--uid", type=int, required=True)

    macro_recording_disable_parser = subparsers.add_parser(
        "disable-macro-recording",
        help="Disable macro recording opt-in",
    )
    macro_recording_disable_parser.add_argument("--uid", type=int, required=True)
    macro_recording_disable_persistent_parser = subparsers.add_parser(
        "disable-macro-recording-persistent",
        help="Disable the persistent macro recording opt-in lease",
    )
    macro_recording_disable_persistent_parser.add_argument("--uid", type=int, required=True)

    args = parser.parse_args()

    try:
        caller_euid = _require_privileged_caller()
        if hasattr(args, "uid"):
            _authorize_target_uid(int(args.uid), caller_euid)

        if args.command == "status":
            status = resolve_unlock_status(args.uid)
            print(json.dumps({"status": "ok", **status}))
            return

        if args.command == "unlock-runtime":
            runtime_path = runtime_unlock_path(args.uid)
            expires_at = _runtime_expires_at(args.ttl)
            _write_lease(runtime_path, expires_at, args.uid)
            print(json.dumps({"status": "ok", "scope": "runtime", "expires_at": expires_at}))
            return

        if args.command == "lock-runtime":
            _remove_lease(runtime_unlock_path(args.uid))
            print(json.dumps({"status": "ok", "scope": "runtime", "locked": True}))
            return

        if args.command == "macro-recording-status":
            status = resolve_macro_recording_status(args.uid)
            print(json.dumps({"status": "ok", **status}))
            return

        if args.command == "enable-macro-recording-runtime":
            runtime_path = runtime_macro_recording_path(args.uid)
            expires_at = _runtime_expires_at(args.ttl)
            _write_lease(runtime_path, expires_at, args.uid)
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "scope": "macro_recording_runtime",
                        "expires_at": expires_at,
                    }
                )
            )
            return

        if args.command == "enable-macro-recording-persistent":
            _write_lease(persistent_macro_recording_path(args.uid), 0, args.uid)
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "scope": "macro_recording_persistent",
                        "expires_at": 0,
                    }
                )
            )
            return

        if args.command == "disable-macro-recording":
            _remove_lease(runtime_macro_recording_path(args.uid))
            _remove_lease(persistent_macro_recording_path(args.uid))
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "scope": "macro_recording",
                        "enabled": False,
                    }
                )
            )
            return

        if args.command == "disable-macro-recording-persistent":
            _remove_lease(persistent_macro_recording_path(args.uid))
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "scope": "macro_recording_persistent",
                        "enabled": False,
                    }
                )
            )
            return

        raise ValueError("Unknown command")
    except (PermissionError, OSError, ValueError) as exc:
        _exit_error(exc)
    except Exception as exc:
        log.exception("Unexpected keymasq-record failure")
        _exit_error(exc)


if __name__ == "__main__":
    main()
