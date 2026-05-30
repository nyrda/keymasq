from keymasq.common.asyncio_runtime import ensure_uvloop


def main() -> None:
    ensure_uvloop()

    from keymasq.session.manager import main as manager_main

    manager_main()


if __name__ == "__main__":
    main()
