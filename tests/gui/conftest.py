# pyright: reportUnusedFunction=false

import pytest


@pytest.fixture(autouse=True)
def _reset_session_compositor_cache():
    from keymasq.gui import compositor_state

    compositor_state.update_session_compositor_id(None)
    yield
    compositor_state.update_session_compositor_id(None)
