import argparse
import json
import logging
import sys

log = logging.getLogger("keymasq.helper")


def _exit_error(message: object) -> None:
    print(json.dumps({"status": "error", "message": str(message)}))
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="keymasq-helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    hardware = subparsers.add_parser("hardware-operation", help=argparse.SUPPRESS)
    hardware.add_argument("request_id")
    subparsers.add_parser("recover-hardware", help="Restore hardware access after daemon failure")
    subparsers.add_parser(
        "prepare-removal",
        help="Stop keymasqd and restore hardware access before package removal",
    )

    args = parser.parse_args()

    try:
        from keymasq.masking.operations import main as hardware_operation

        hardware_operation(args.command, getattr(args, "request_id", ""))
    except (PermissionError, OSError, ValueError) as exc:
        _exit_error(exc)
    except Exception as exc:
        log.exception("Unexpected keymasq-helper failure")
        _exit_error(exc)


if __name__ == "__main__":
    main()
