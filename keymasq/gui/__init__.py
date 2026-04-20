from typing import TYPE_CHECKING, Any

__all__ = ["Application", "MainWindow", "main"]

if TYPE_CHECKING:
    from keymasq.gui.application import Application, main
    from keymasq.gui.window import MainWindow


def __getattr__(name: str) -> Any:
    if name in {"Application", "main"}:
        from keymasq.gui.application import Application, main

        exports = {
            "Application": Application,
            "main": main,
        }
        return exports[name]

    if name == "MainWindow":
        from keymasq.gui.window import MainWindow

        return MainWindow

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
