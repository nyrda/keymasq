import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

_ADW_FLOATING_SHEET_WIDTH_OVERHEAD = 70


def parent_constrained_dialog_width(
    parent: Gtk.Widget,
    preferred_width: int,
    *,
    min_width: int = 360,
) -> int:
    root = parent.get_root()
    width_source = root if isinstance(root, Gtk.Widget) else parent
    allocated_width = width_source.get_width()
    if allocated_width <= 0:
        return preferred_width
    max_width = max(min_width, allocated_width - _ADW_FLOATING_SHEET_WIDTH_OVERHEAD)
    return min(preferred_width, max_width)
