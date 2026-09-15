"""Existing masking paths and reservation identity validation."""

import re
from pathlib import Path

RUNTIME_DIR = Path("/run/keymasq-masking")
STATE_DIR = Path("/var/lib/keymasq-masking")
POLICY_DIR = Path("/var/lib/keymasq/masking")


def validate_identity(identity: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{24}", identity):
        raise ValueError("Invalid attachment identity")


def reservation_ids(*roots: Path) -> list[str]:
    return sorted(
        {
            path.name
            for root in roots
            for path in (root / "reservations").glob("*")
            if re.fullmatch(r"[0-9a-f]{24}", path.name) and path.is_dir()
        }
    )
