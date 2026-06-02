from pathlib import Path

from tests.conftest import _category_for_test_path


def test_collection_category_covers_major_test_subtrees() -> None:
    assert _category_for_test_path(Path("tests/common/test_macro_compile.py")) == "common"
    assert _category_for_test_path(Path("tests/keymasqd/test_daemon.py")) == "keymasqd"
    assert _category_for_test_path(Path("tests/session/test_session_clients.py")) == "session"
    assert _category_for_test_path(Path("tests/gui/test_action_labels.py")) == "gui"


def test_collection_category_uses_repo_tests_segment_for_absolute_paths() -> None:
    path = Path("/tmp/tests/work/keymasq/tests/common/test_macro_compile.py")

    assert _category_for_test_path(path) == "common"


def test_collection_category_does_not_use_root_level_basename_fallback() -> None:
    assert _category_for_test_path(Path("tests/test_daemon.py")) is None


def test_collection_category_uses_top_level_filename_before_ancestor_category() -> None:
    assert (
        _category_for_test_path(Path("/tmp/tests/common/keymasq/tests/test_daemon.py"))
        == "keymasqd"
    )
    assert (
        _category_for_test_path(Path("/tmp/tests/keymasqd/keymasq/tests/test_session_clients.py"))
        == "session"
    )
