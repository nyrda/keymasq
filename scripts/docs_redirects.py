"""Keep public URLs from before the lowercase documentation rename working."""

import json
from collections.abc import Mapping
from html import escape
from pathlib import Path
from typing import Any

# Only pages renamed in September 2026 get legacy aliases.
_RENAMED_PAGES = (
    "actions",
    "analog-controls",
    "cli",
    "combos",
    "daemon-session-integration-test",
    "dependencies",
    "device-inspector",
    "examples",
    "gamepad",
    "getting-started",
    "gnome",
    "hardware",
    "hardware-masking",
    "hardware-masking-design",
    "input-driver-design",
    "install",
    "listener-vm-tests",
    "macro-editor",
    "macros",
    "motion-controls",
    "packaging",
    "performance",
    "playback-requests",
    "profiles",
    "screenshots",
    "security",
    "steamos",
    "superkeys",
    "troubleshooting",
    "vm-testing",
    "wayland",
)


def on_post_build(config: Mapping[str, Any], **_kwargs: Any) -> None:
    site_dir = Path(config["site_dir"])
    directory_urls = config["use_directory_urls"]
    for slug in _RENAMED_PAGES:
        old_slug = slug.upper().replace("-", "_")
        if directory_urls:
            destination = site_dir / old_slug / "index.html"
            target = f"../{slug}/"
        else:
            destination = site_dir / f"{old_slug}.html"
            target = f"{slug}.html"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
            '<title>Page moved</title>\n'
            f'<link rel="canonical" href="{escape(target, quote=True)}">\n'
            '<script>location.replace('
            f'{json.dumps(target)} + location.search + location.hash);</script>\n'
            '<noscript><meta http-equiv="refresh" '
            f'content="0; url={escape(target, quote=True)}"></noscript>\n'
            '</head><body><p>This page has moved to '
            f'<a href="{escape(target, quote=True)}">{escape(slug)}</a>.'
            '</p></body></html>\n',
            encoding="utf-8",
        )
