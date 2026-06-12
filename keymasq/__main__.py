import os
import sys


def _has_desktop_environment() -> bool:
    for name in ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "WAYLAND_DISPLAY", "DISPLAY"):
        value = os.environ.get(name, "").strip()
        if value:
            return True
    return False


def main() -> None:
    if len(sys.argv) > 1 or not _has_desktop_environment():
        from keymasq.cli.__main__ import main as cli_main

        cli_main()
        return

    from keymasq.common.asyncio_runtime import ensure_uvloop
    from keymasq.gui.__main__ import main as gui_main

    ensure_uvloop()
    gui_main()


if __name__ == "__main__":
    main()
