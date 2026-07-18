import logging
import re
from typing import cast

from keymasq.common.model.profiles import (
    ProfileConfig,
    WindowRule,
)

from .types import TomlDict

log = logging.getLogger("keymasq-session.profiles")
SUPPORTED_WINDOW_RULE_FIELDS = frozenset({"class", "title", "tag"})


def normalize_window_rule_field(value: object) -> str:
    field = str(value).strip().lower()
    return "tag" if field == "tags" else field


def has_unsupported_rules(config: ProfileConfig, capabilities: list[str]) -> bool:
    return "window_tags" not in capabilities and any(
        normalize_window_rule_field(rule.field) == "tag" for rule in config.window_rules
    )


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
            elif field in {"class", "title"}:
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
