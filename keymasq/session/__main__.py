import argparse
import sys

from keymasq.common.asyncio_runtime import ensure_uvloop


def _wants_help(argv: list[str]) -> bool:
    return any(arg in {"-h", "--help"} for arg in argv[1:])


def _print_help() -> None:
    parser = argparse.ArgumentParser(
        prog="keymasq-session",
        description="Keymasq Session Manager",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Enable debug logging (-v) or trace logging (-vv)",
    )
    parser.print_help()


def main() -> None:
    ensure_uvloop()

    if _wants_help(sys.argv):
        _print_help()
        return

    from keymasq.session.manager import main as manager_main

    manager_main()


if __name__ == "__main__":
    main()
