from keymasq.common.asyncio_runtime import ensure_uvloop


def main() -> None:
    ensure_uvloop()

    from keymasq.keymasqd.daemon import main

    main()


if __name__ == "__main__":
    main()
