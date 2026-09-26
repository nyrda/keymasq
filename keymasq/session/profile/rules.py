import logging
import re
from collections.abc import Collection, Iterable
from typing import cast

from keymasq.common.model.profiles import (
    ProfileConfig,
    WindowRule,
)

from .types import ProfileInfo, TomlDict

log = logging.getLogger("keymasq-session.profiles")
SUPPORTED_WINDOW_RULE_FIELDS = frozenset({"class", "title", "tag", "layer"})
WINDOW_MATCH_FIELDS = frozenset({"class", "title", "tag", "layer"})
WINDOW_RULE_FIELD_CAPABILITIES = {"tag": "window_tags", "layer": "layer_focus"}


def normalize_window_rule_field(value: object) -> str:
    field = str(value).strip().lower()
    return "tag" if field == "tags" else field


def window_rule_field_supported(field: str, capabilities: Collection[str]) -> bool:
    capability = WINDOW_RULE_FIELD_CAPABILITIES.get(normalize_window_rule_field(field))
    return capability is None or capability in capabilities


def normalize_window_info_for_match(
    window_info: TomlDict | None,
) -> dict[str, str | tuple[str, ...]]:
    """Normalize window state for match-relevant comparison.

    Missing keys, empty strings, tag order, and blank tags all fail to match
    the same way, so they compare equal here.
    """
    if not isinstance(window_info, dict):
        return {"class": "", "title": "", "tag": (), "layer": ""}
    window_class = window_info.get("class", "")
    window_title = window_info.get("title", "")
    layer = window_info.get("layer", "")
    raw_tags = window_info.get("tags", [])
    if not isinstance(raw_tags, list):
        raw_tags = []
    typed_tags = cast(list[object], raw_tags)
    tags = sorted(
        {str(tag) for tag in typed_tags if str(tag or "").strip()},
    )
    return {
        "class": window_class if isinstance(window_class, str) else "",
        "title": window_title if isinstance(window_title, str) else "",
        "tag": tuple(tags),
        "layer": layer if isinstance(layer, str) else "",
    }


def has_unsupported_rules(config: ProfileConfig, capabilities: list[str]) -> bool:
    return any(
        not window_rule_field_supported(rule.field, capabilities) for rule in config.window_rules
    )


def window_fields_in_use(
    profiles: Iterable[ProfileInfo],
    capabilities: list[str] | None = None,
) -> frozenset[str]:
    """Return normalized window fields that can change conditional profile matches.

    Only enabled conditional profiles whose rules are supported on the current
    compositor can change the resolved profile set, so a window update touching
    none of these fields cannot alter profile resolution.
    """
    capable = capabilities or []
    fields: set[str] = set()
    for info in profiles:
        config = info.config
        if not config.enabled or config.is_permanent or not config.window_rules:
            continue
        if has_unsupported_rules(config, capable):
            continue
        for rule in config.window_rules:
            field = normalize_window_rule_field(rule.field)
            if field in WINDOW_MATCH_FIELDS:
                fields.add(field)
    return frozenset(fields)


def validate_window_rules(window_rules: list[WindowRule]) -> None:
    for index, rule in enumerate(window_rules, start=1):
        try:
            re.compile(rule.pattern)
        except re.error as exc:
            raise ValueError(
                f"Invalid regex in window rule {index} for field '{rule.field}': {exc}"
            ) from exc


def matches_window_rules(profile: ProfileConfig, window_info: TomlDict | None) -> bool:
    if not profile.window_rules or not window_info:
        return False

    for rule in profile.window_rules:
        try:
            field = normalize_window_rule_field(rule.field)
            if field == "tag":
                window_tags = window_info.get("tags", [])
                if not isinstance(window_tags, list):
                    window_tags = []
                tags = cast(list[object], window_tags)
                if not any(re.search(rule.pattern, str(tag)) for tag in tags):
                    return False
            elif field in {"class", "title", "layer"}:
                field_value = window_info.get(field, "")
                if not isinstance(field_value, str):
                    return False
                if not field_value or not re.search(rule.pattern, field_value):
                    return False
            else:
                return False
        except re.error as exc:
            log.warning(
                "Invalid window rule regex for profile '%s' field '%s': %s",
                profile.name,
                rule.field,
                exc,
            )
            return False

    return True
