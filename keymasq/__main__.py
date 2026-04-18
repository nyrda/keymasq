import os
import sys

from keymasq.common.asyncio_runtime import ensure_uvloop


def _has_desktop_environment() -> bool:
    for name in ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "WAYLAND_DISPLAY", "DISPLAY"):
        value = os.environ.get(name, "").strip()
        if value:
            return True
    return False


def main() -> None:
    ensure_uvloop()

    if len(sys.argv) > 1:
        from keymasq.cli.__main__ import main as cli_main

        cli_main()
        return

    if _has_desktop_environment():
        from keymasq.gui.__main__ import main as gui_main

        gui_main()
        return

    from keymasq.cli.__main__ import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
