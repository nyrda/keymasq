from pathlib import Path

import pytest


def test_collected_tests_have_their_directory_category(request: pytest.FixtureRequest) -> None:
    tests_dir = Path(__file__).resolve().parents[1]
    categories = {"common", "keymasqd", "session", "gui"}
    for item in request.session.items:
        directory = item.path.relative_to(tests_dir).parts[0]
        assert directory in categories, item.nodeid
        assert [
            marker.name for marker in item.iter_markers() if marker.name in categories
        ] == [directory], item.nodeid
