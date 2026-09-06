from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from typing import Any, cast

from keymasq.common.coercion import coerce_bool, coerce_float, coerce_int, coerce_str
from keymasq.common.model.actions import (
    DEFAULT_MACRO_LOOP_STOP_BEHAVIOR,
    normalize_macro_loop_stop_behavior,
)

type _MacroOptionParser = Callable[[object, object, bool], object]

_PLAYBACK_PARSER_METADATA = "parser"
_RUNTIME_DEFAULT_METADATA = "runtime_default"


def _playback_metadata(
    parser: _MacroOptionParser,
    *,
    runtime_default: bool = False,
) -> dict[str, object]:
    return {
        _PLAYBACK_PARSER_METADATA: parser,
        _RUNTIME_DEFAULT_METADATA: runtime_default,
    }


def _parse_runtime_text(value: object, default: object, _lenient: bool) -> str:
    default_text = coerce_str(default, "none") or "none"
    return coerce_str(value, default_text) or default_text


def _parse_playback_text(value: object, default: object, _lenient: bool) -> str:
    return coerce_str(value, coerce_str(default))


def _parse_playback_int(value: object, default: object, lenient: bool) -> int:
    default_int = coerce_int(default, 0)
    if lenient:
        return coerce_int(value, default_int)
    if _is_missing_playback_number(value):
        return default_int
    if isinstance(value, bool):
        raise ValueError("Boolean values are not accepted")
    return int(cast(int | float | str | bytes, value))


def _parse_playback_float(value: object, default: object, _lenient: bool) -> float:
    return coerce_float(value, coerce_float(default, 1.0))


def _is_missing_playback_number(value: object) -> bool:
    return value is None or (isinstance(value, str) and value == "")


def _parse_playback_bool(value: object, default: object, lenient: bool) -> bool:
    return coerce_bool(value, coerce_bool(default), strict=not lenient)


def _parse_runtime_loop_stop_behavior(
    value: object,
    _default: object,
    _lenient: bool,
) -> str:
    return normalize_macro_loop_stop_behavior(value)


@dataclass(frozen=True)
class MacroPlaybackOptions:
    """Validated options for one macro playback request."""

    macro_events: list[dict[str, object]] = field(default_factory=list)
    macro_name: str = ""
    replay_mouse_movement: bool = field(
        default=True,
        metadata=_playback_metadata(_parse_playback_bool),
    )
    replay_mouse_clicks: bool = field(
        default=True,
        metadata=_playback_metadata(_parse_playback_bool),
    )
    speed: float = field(
        default=1.0,
        metadata=_playback_metadata(_parse_playback_float),
    )
    loop_mode: str = field(
        default="none",
        metadata=_playback_metadata(_parse_runtime_text, runtime_default=True),
    )
    loop_count: int = field(
        default=1,
        metadata=_playback_metadata(_parse_playback_int, runtime_default=True),
    )
    loop_stop_behavior: str = field(
        default=DEFAULT_MACRO_LOOP_STOP_BEHAVIOR,
        metadata=_playback_metadata(
            _parse_runtime_loop_stop_behavior,
            runtime_default=True,
        ),
    )
    move_to_start: bool = field(
        default=False,
        metadata=_playback_metadata(_parse_playback_bool, runtime_default=True),
    )
    start_x: int = field(
        default=0,
        metadata=_playback_metadata(_parse_playback_int, runtime_default=True),
    )
    start_y: int = field(
        default=0,
        metadata=_playback_metadata(_parse_playback_int, runtime_default=True),
    )
    block_mouse_movement: bool = field(
        default=False,
        metadata=_playback_metadata(_parse_playback_bool, runtime_default=True),
    )
    source_device: str = field(
        default="",
        metadata=_playback_metadata(_parse_playback_text),
    )
    source_button: str = field(
        default="",
        metadata=_playback_metadata(_parse_playback_text),
    )
    trigger_value: int = field(
        default=1,
        metadata=_playback_metadata(_parse_playback_int),
    )
    playback_id: str = field(default="", metadata=_playback_metadata(_parse_playback_text))
    load_stored_macro: bool = True


def macro_runtime_options(
    payload: Mapping[str, object],
    *,
    defaults: Mapping[str, object] | None = None,
    lenient: bool = True,
) -> dict[str, object]:
    return _macro_options_from_fields(
        payload,
        defaults=defaults,
        lenient=lenient,
        runtime_only=True,
    )


def macro_playback_options_from_mapping(
    playback_options: Mapping[str, object],
    *,
    defaults: Mapping[str, object] | None = None,
    lenient: bool = True,
    strict: bool = False,
    macro_events: list[dict[str, object]] | None = None,
    macro_name: str | None = None,
    load_stored_macro: bool | None = None,
) -> MacroPlaybackOptions:
    playback_fields = fields(MacroPlaybackOptions)
    option_names = {option_field.name for option_field in playback_fields}
    unexpected_options = sorted(set(playback_options) - option_names) if strict else []
    if unexpected_options:
        raise TypeError(f"unexpected playback option: {unexpected_options[0]}")

    option_values = _macro_options_from_fields(
        playback_options,
        defaults=defaults,
        lenient=lenient,
        runtime_only=False,
    )
    for option_field in playback_fields:
        if _playback_option_parser(option_field.metadata) is None:
            option_name = option_field.name
            if option_name in playback_options:
                option_values[option_name] = playback_options[option_name]
    if macro_events is not None:
        option_values["macro_events"] = macro_events
    if macro_name is not None:
        option_values["macro_name"] = macro_name
    if load_stored_macro is not None:
        option_values["load_stored_macro"] = load_stored_macro
    return MacroPlaybackOptions(**cast(Any, option_values))


def _macro_options_from_fields(
    payload: Mapping[str, object],
    *,
    defaults: Mapping[str, object] | None,
    lenient: bool,
    runtime_only: bool,
) -> dict[str, object]:
    options: dict[str, object] = {}
    for option_field in fields(MacroPlaybackOptions):
        parser = _playback_option_parser(option_field.metadata)
        if parser is None:
            continue
        if runtime_only and not bool(option_field.metadata.get(_RUNTIME_DEFAULT_METADATA, False)):
            continue

        option_name = option_field.name
        default = (
            defaults.get(option_name, option_field.default)
            if defaults is not None
            else option_field.default
        )
        options[option_name] = parser(payload.get(option_name, default), default, lenient)
    return options


def _playback_option_parser(
    metadata: Mapping[str, object],
) -> _MacroOptionParser | None:
    return cast(_MacroOptionParser | None, metadata.get(_PLAYBACK_PARSER_METADATA))
