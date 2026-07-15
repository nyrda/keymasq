from keymasq.common.asyncio_runtime import ensure_uvloop


def main() -> None:
    ensure_uvloop()

    from keymasq.gui import application

    application.main()


if __name__ == "__main__":
    main()
