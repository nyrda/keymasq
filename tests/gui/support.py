def iter_widget_children(widget):
    if widget is None:
        return
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


def iter_widget_tree(widget, *, include_self: bool = False):
    if widget is None:
        return
    if include_self:
        yield widget
    for child in iter_widget_children(widget):
        yield child
        yield from iter_widget_tree(child)


def collect_child_widgets(widget, widget_type):
    return [child for child in iter_widget_children(widget) if isinstance(child, widget_type)]


def collect_widgets(widget, widget_type, *, include_self: bool = False):
    return [
        child
        for child in iter_widget_tree(widget, include_self=include_self)
        if isinstance(child, widget_type)
    ]


def _first_widget_label(widget):
    for child in iter_widget_tree(widget, include_self=True):
        get_label = getattr(child, "get_label", None)
        if callable(get_label):
            return get_label()
    raise AssertionError("widget tree does not contain a label")


def collect_listbox_row_labels(listbox):
    return [_first_widget_label(row.get_child()) for row in iter_widget_children(listbox)]


__all__ = [
    "iter_widget_children",
    "collect_child_widgets",
    "collect_listbox_row_labels",
    "collect_widgets",
    "iter_widget_tree",
]
