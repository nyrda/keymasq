import pytest

from tests.keymasqd.daemon_support import make_daemon_testbed


@pytest.fixture
def daemon_testbed(monkeypatch):
    return make_daemon_testbed(monkeypatch)
