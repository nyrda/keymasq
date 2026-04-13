# pyright: reportUnusedImport=false, reportUnusedFunction=false, reportUnusedClass=false
# ruff: noqa: F401, I001
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

import keyforge.session.manager.compositor as session_compositor_module
import keyforge.session.manager.profiles as session_profiles_module
import keyforge.session.manager.recording as session_recording_module
from keyforge.common.ipc import Response
from keyforge.common.security import PeerCredentials, SecurityPolicy
from keyforge.session.listeners.hyprland import HyprlandListener
from keyforge.session.listeners.kde import KDEListener
from keyforge.session.manager import SessionManager

__all__ = [
    'SimpleNamespace',
    'cast',
    'AsyncMock',
    'Mock',
    'pytest',
    'session_compositor_module',
    'session_profiles_module',
    'session_recording_module',
    'Response',
    'PeerCredentials',
    'SecurityPolicy',
    'HyprlandListener',
    'KDEListener',
    'SessionManager',
]
