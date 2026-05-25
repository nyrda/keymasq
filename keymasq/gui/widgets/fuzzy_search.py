import re
from collections.abc import Callable, Mapping

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

_TOKEN_RE = re.compile(r"[a-z0-9]+")


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


def install_listbox_fuzzy_filter(
    listbox: Gtk.ListBox,
    search_entry: Gtk.Editable,
    *,
    row_text_attr: str = "_search_text",
    before_filter_changed: Callable[[], None] | None = None,
    after_filter_changed: Callable[[], None] | None = None,
) -> None:
    state = {"query": search_entry.get_text()}

    def filter_row(row: Gtk.ListBoxRow) -> bool:
        return fuzzy_query_matches(state["query"], getattr(row, row_text_attr, ""))

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
