"""Resolved rollover group payloads and their reconciliation signature."""

import json
from typing import cast

from keymasq.common.model.profiles import RolloverGroup
from keymasq.common.rollover import rollover_group_to_data

from ..common import JsonObject


def serialize(groups: list[RolloverGroup]) -> list[JsonObject]:
    return [cast(JsonObject, rollover_group_to_data(group)) for group in groups]


def signature(groups: list[RolloverGroup]) -> str:
    return json.dumps(serialize(groups), sort_keys=True, separators=(",", ":"))


EMPTY_SIGNATURE = signature([])
# The daemon's groups are not known, for example after a hotplug refresh or an
# interrupted send, so the next groups are sent whatever they are.
UNKNOWN_SIGNATURE = ""
