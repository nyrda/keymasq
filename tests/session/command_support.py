# pyright: reportUnusedImport=false, reportUnusedFunction=false, reportUnusedClass=false
# ruff: noqa: F401, I001
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

import keymasq.session.manager.compositor as session_compositor_module
import keymasq.session.manager.profiles as session_profiles_module
import keymasq.session.manager.recording as session_recording_module
from keymasq.common.ipc import Response
from keymasq.common.security import PeerCredentials, SecurityPolicy
from keymasq.session.listeners.hyprland import HyprlandListener
from keymasq.session.listeners.kde import KDEListener
from keymasq.session.manager import SessionManager

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
