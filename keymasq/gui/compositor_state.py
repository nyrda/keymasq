"""Session-sourced compositor identity shared across GUI widgets.

keymasq-session is the authority on the running compositor. The GUI process
may run in a different environment (AppImage, bundled waypipe, remote
display), so it must never probe the compositor itself; it caches what the
session broker reports and widgets read that cache.
"""

from __future__ import annotations

_compositor_id: str | None = None


def update_session_compositor_id(compositor_id: object) -> None:
    global _compositor_id
    _compositor_id = compositor_id if isinstance(compositor_id, str) and compositor_id else None


def session_compositor_id() -> str | None:
    return _compositor_id
