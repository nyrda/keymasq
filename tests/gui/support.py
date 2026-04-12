# pyright: reportUnusedImport=false, reportUnusedFunction=false, reportUnusedClass=false
# ruff: noqa: F401, I001
from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")

__all__ = [
    'SimpleNamespace',
    'pytest',
    'gi',
]
