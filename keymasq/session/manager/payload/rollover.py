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


# The daemon drops its groups when the session disconnects, so a new connection
# starts from no groups.
EMPTY_SIGNATURE = signature([])
