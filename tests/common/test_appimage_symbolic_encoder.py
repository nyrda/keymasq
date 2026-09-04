from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENCODER = ROOT / "packaging/appimage/encode-symbolic-icon.py"


def test_symbolic_encoder_preserves_foreground_and_warning_planes(tmp_path: Path) -> None:
    gi = pytest.importorskip("gi")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    source = tmp_path / "mixed-symbolic.svg"
    output = tmp_path / "mixed-symbolic.symbolic.png"
    source.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">
<rect x="0" y="0" width="8" height="16" fill="#2e3436"/>
<rect class="warning" x="8" y="0" width="8" height="16" fill="#ff7800"/>
</svg>
""",
        encoding="utf-8",
    )

    subprocess.run(
        [sys.executable, str(ENCODER), str(source), str(output), "32"],
        check=True,
    )

    pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(output))
    assert (pixbuf.get_width(), pixbuf.get_height()) == (32, 32)
    pixels = bytes(pixbuf.get_pixels())
    rowstride = pixbuf.get_rowstride()

    def rgba(x: int, y: int) -> tuple[int, int, int, int]:
        offset = y * rowstride + x * 4
        return tuple(pixels[offset : offset + 4])  # type: ignore[return-value]

    assert rgba(8, 16) == (0, 0, 0, 255)
    assert rgba(24, 16) == (0, 255, 0, 255)
