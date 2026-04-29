from __future__ import annotations

from collections.abc import Callable, Mapping

from keymasq.common.models import MappingAction

from .compositors import COMPOSITOR_ACTION_DEFINITIONS
from .core import (
    build_compositor_action_pages_for_definitions,
    compositor_action_tab_name_for_definitions,
    describe_compositor_action_for_definitions,
)


def build_compositor_action_pages(
    current_action: MappingAction | None,
    on_selected: Callable[[MappingAction], None],
    status: Mapping[str, object] | None = None,
    submit_label: str | None = None,
):
    return build_compositor_action_pages_for_definitions(
        COMPOSITOR_ACTION_DEFINITIONS,
        current_action,
        on_selected,
        status,
        submit_label,
    )


def compositor_action_tab_name(
    action: MappingAction | None,
    status: Mapping[str, object] | None = None,
) -> str | None:
    return compositor_action_tab_name_for_definitions(
        COMPOSITOR_ACTION_DEFINITIONS,
        action,
        status,
    )


def describe_compositor_action(action: MappingAction) -> str | None:
    return describe_compositor_action_for_definitions(
        COMPOSITOR_ACTION_DEFINITIONS,
        action,
    )
