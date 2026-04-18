from keymasq.common.asyncio_runtime import ensure_uvloop


def main() -> None:
    ensure_uvloop()

    from keymasq.gui.application import main as gui_main

    gui_main()


if __name__ == "__main__":
    main()
