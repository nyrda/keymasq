"""Read macro fragments from the native clipboard without blocking GTK."""

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gio, GLib  # pyright: ignore[reportAttributeAccessIssue]

MACRO_FRAGMENT_MIME = "application/x-keymasq-macro-fragment"
_MAX_FRAGMENT_BYTES = 64 * 1024 * 1024


def has_macro_fragment(clipboard: Gdk.Clipboard) -> bool:
    return clipboard.get_formats().contain_mime_type(MACRO_FRAGMENT_MIME)


def read_macro_fragment(
    clipboard: Gdk.Clipboard,
    cancellable: Gio.Cancellable,
    callback: Callable[[bytes | None, str | None], None],
) -> None:
    stream: Gio.InputStream | None = None
    chunks = bytearray()
    finished = False

    def timed_out() -> bool:
        nonlocal timeout_id
        timeout_id = 0
        cancellable.cancel()
        return GLib.SOURCE_REMOVE

    timeout_id = GLib.timeout_add_seconds(10, timed_out)

    def finish(data: bytes | None, error: str | None) -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        if timeout_id:
            GLib.source_remove(timeout_id)
        if stream is not None:
            stream.close_async(GLib.PRIORITY_DEFAULT, None, None, None)
        callback(data, error)

    def chunk_ready(source: Gio.InputStream, result: Gio.AsyncResult) -> None:
        try:
            chunk = source.read_bytes_finish(result).get_data()
        except GLib.Error as exc:
            finish(None, str(exc))
            return
        if not chunk:
            finish(bytes(chunks), None)
            return
        if len(chunks) + len(chunk) > _MAX_FRAGMENT_BYTES:
            finish(None, "The copied macro section exceeds the 64 MiB clipboard limit.")
            return
        chunks.extend(chunk)
        source.read_bytes_async(65536, GLib.PRIORITY_DEFAULT, cancellable, chunk_ready)

    def opened(source: Gdk.Clipboard, result: Gio.AsyncResult) -> None:
        nonlocal stream
        try:
            stream, mime = source.read_finish(result)
        except GLib.Error as exc:
            finish(None, str(exc))
            return
        if stream is None or mime != MACRO_FRAGMENT_MIME:
            finish(None, "The clipboard no longer contains macro actions.")
            return
        stream.read_bytes_async(65536, GLib.PRIORITY_DEFAULT, cancellable, chunk_ready)

    clipboard.read_async([MACRO_FRAGMENT_MIME], GLib.PRIORITY_DEFAULT, cancellable, opened)
