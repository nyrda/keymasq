import os
import sys


def _has_desktop_environment() -> bool:
    for name in ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "WAYLAND_DISPLAY", "DISPLAY"):
        value = os.environ.get(name, "").strip()
        if value:
            return True
    return False


def _run_cli() -> None:
    from keymasq.cli.__main__ import main

    main()


def _run_gui() -> None:
    from keymasq.common.asyncio_runtime import ensure_uvloop
    from keymasq.gui.__main__ import main

    ensure_uvloop()
    main()


def main() -> None:
    if len(sys.argv) > 1 or not _has_desktop_environment():
        _run_cli()
        return

    _run_gui()


if __name__ == "__main__":
    main()
