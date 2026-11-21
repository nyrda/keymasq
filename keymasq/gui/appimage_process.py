from __future__ import annotations

from collections.abc import Callable, Sequence

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.appimage import AppImageRuntime

LineCallback = Callable[[str], None]
ExitCallback = Callable[[int, list[str]], None]
LauncherRunner = Callable[[AppImageRuntime, Sequence[str], LineCallback | None, ExitCallback], None]

SPAWN_FAILED = -1
_READ_SIZE = 4096


def run_launcher(
    runtime: AppImageRuntime,
    arguments: Sequence[str],
    on_line: LineCallback | None,
    on_exit: ExitCallback,
) -> None:
    lines: list[str] = []
    launcher = Gio.SubprocessLauncher.new(
        Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE
    )
    launcher.set_environ([f"{key}={value}" for key, value in runtime.environment().items()])
    try:
        process = launcher.spawnv(runtime.command(*arguments))
    except GLib.Error as exc:
        on_exit(SPAWN_FAILED, [f"error: {exc.message}"])
        return

    stream = process.get_stdout_pipe()
    pending = bytearray()

    def emit(raw_line: bytes) -> None:
        line = raw_line.decode("utf-8", errors="replace")
        lines.append(line)
        if on_line is not None:
            on_line(line)

    def read_next() -> None:
        stream.read_bytes_async(_READ_SIZE, GLib.PRIORITY_DEFAULT, None, on_chunk_ready)

    def on_chunk_ready(source: Gio.InputStream, result: Gio.AsyncResult) -> None:
        try:
            chunk = source.read_bytes_finish(result).get_data() or b""
        except GLib.Error:
            chunk = b""
        if not chunk:
            if pending:
                emit(bytes(pending))
            process.wait_async(None, on_wait)
            return
        pending.extend(chunk)
        *complete, rest = bytes(pending).split(b"\n")
        pending[:] = rest
        for raw_line in complete:
            emit(raw_line)
        read_next()

    def on_wait(waited: Gio.Subprocess, result: Gio.AsyncResult) -> None:
        try:
            waited.wait_finish(result)
        except GLib.Error:
            on_exit(SPAWN_FAILED, lines)
            return
        on_exit(waited.get_exit_status() if waited.get_if_exited() else SPAWN_FAILED, lines)

    read_next()
