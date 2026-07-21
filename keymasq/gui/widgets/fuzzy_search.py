import re
from collections.abc import Callable, Mapping

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.superkeys import SuperkeyConfig

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_SEARCH_BLOCKING_MODIFIERS = (
    Gdk.ModifierType.CONTROL_MASK
    | Gdk.ModifierType.ALT_MASK
    | Gdk.ModifierType.SUPER_MASK
    | Gdk.ModifierType.META_MASK
)


def _normalize_search_text(value: object) -> str:
    return " ".join(_TOKEN_RE.findall(str(value).casefold()))


def _is_subsequence(needle: str, haystack: str) -> bool:
    if not needle:
        return True
    index = 0
    for char in haystack:
        if char == needle[index]:
            index += 1
            if index == len(needle):
                return True
    return False


def _token_matches(needle: str, haystack_tokens: list[str]) -> bool:
    initials = "".join(token[0] for token in haystack_tokens if token)
    if any(
        needle in token or (len(needle) > 1 and _is_subsequence(needle, token))
        for token in haystack_tokens
    ):
        return True
    if len(needle) <= 1:
        return False
    if needle in initials:
        return True
    for start in range(len(haystack_tokens)):
        for end in range(start + 2, min(start + 4, len(haystack_tokens) + 1)):
            if _is_subsequence(needle, "".join(haystack_tokens[start:end])):
                return True
    return False


def fuzzy_query_matches(query: str, text: object) -> bool:
    normalized_query = _normalize_search_text(query)
    if not normalized_query:
        return True

    normalized_text = _normalize_search_text(text)
    if not normalized_text:
        return False

    text_tokens = normalized_text.split()
    for token in normalized_query.split():
        if _token_matches(token, text_tokens):
            continue
        return False
    return True


def macro_search_text(macro: Mapping[str, object]) -> str:
    event_count = macro.get("event_count", "")
    device_types_value = macro.get("device_types", [])
    device_types = (
        " ".join(str(device_type) for device_type in device_types_value)
        if isinstance(device_types_value, list)
        else str(device_types_value)
    )
    duration_value = macro.get("duration_us", 0)
    try:
        duration_us = int(duration_value) if isinstance(duration_value, int | float | str) else 0
    except ValueError:
        duration_us = 0
    duration_ms = duration_us // 1000
    return f"{macro.get('name', '')} {device_types} {event_count} events {duration_ms}ms"


def superkey_search_text(config: SuperkeyConfig | None, name: str) -> str:
    if config is None:
        return name
    return " ".join(
        [
            str(config.name or ""),
            str(config.description or ""),
            config.mode.value,
            str(len(config.tap_actions)),
            str(len(config.double_tap_actions)),
            str(len(config.hold_actions)),
            str(len(config.tap_hold_actions)),
            str(len(config.overload_actions)),
            str(len(config.overload_down_actions)),
            str(len(config.overload_up_actions)),
            "actions",
        ]
    )


def start_search_from_keypress(
    owner: Gtk.Widget,
    search_entry: Gtk.SearchEntry,
    keyval: int,
    state: Gdk.ModifierType | int,
    *,
    show_search: Callable[[], None] | None = None,
) -> bool:
    """Start a search with an unmodified printable key.

    Existing text inputs retain normal typing behavior. This lets dialogs use
    type-to-search without stealing input from editor fields.
    """
    if state & _SEARCH_BLOCKING_MODIFIERS:
        return False
    if _text_input_has_focus(owner):
        return False

    unicode_value = Gdk.keyval_to_unicode(keyval)
    if not unicode_value:
        return False
    char = chr(unicode_value)
    if not char.isprintable() or char.isspace():
        return False

    search_was_visible = search_entry.get_visible()
    existing_text = search_entry.get_text() if search_was_visible else ""
    if search_was_visible:
        search_entry.grab_focus()
    elif show_search is not None:
        show_search()
    else:
        return False
    if not search_entry.get_visible():
        return False
    search_entry.set_text(existing_text + char)
    search_entry.set_position(-1)
    return True


def _text_input_has_focus(owner: Gtk.Widget) -> bool:
    root = owner.get_root()
    focus = root.get_focus() if root is not None else None
    while focus is not None:
        if isinstance(focus, Gtk.Editable | Gtk.TextView):
            return True
        focus = focus.get_parent()
    return False


def install_listbox_fuzzy_filter(
    listbox: Gtk.ListBox,
    search_entry: Gtk.Editable,
    *,
    row_text_attr: str = "_search_text",
    row_text: Callable[[Gtk.ListBoxRow], object] | None = None,
    row_matches: Callable[[str, Gtk.ListBoxRow], bool] | None = None,
    before_filter_changed: Callable[[], None] | None = None,
    after_filter_changed: Callable[[], None] | None = None,
) -> None:
    state = {"query": search_entry.get_text()}

    def filter_row(row: Gtk.ListBoxRow) -> bool:
        if row_matches is not None:
            return row_matches(state["query"], row)
        text = row_text(row) if row_text is not None else getattr(row, row_text_attr, "")
        return fuzzy_query_matches(state["query"], text)

    def on_search_changed(entry: Gtk.Editable) -> None:
        state["query"] = entry.get_text()
        if before_filter_changed is not None:
            before_filter_changed()
        try:
            listbox.invalidate_filter()
        finally:
            if after_filter_changed is not None:
                after_filter_changed()

    listbox.set_filter_func(filter_row)
    search_entry.connect("changed", on_search_changed)
