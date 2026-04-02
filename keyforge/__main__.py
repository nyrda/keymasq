import os
import sys


def _has_desktop_environment() -> bool:
    for name in ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "WAYLAND_DISPLAY", "DISPLAY"):
        value = os.environ.get(name, "").strip()
        if value:
            return True
    return False


def main() -> None:
    if len(sys.argv) > 1:
        from keyforge.cli.__main__ import main as cli_main

        cli_main()
        return

    if _has_desktop_environment():
        from keyforge.gui.__main__ import main as gui_main

        gui_main()
        return

    from keyforge.cli.__main__ import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
