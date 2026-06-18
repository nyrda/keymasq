from pathlib import Path

from tests.conftest import _CATEGORY_BY_FILE, _category_for_test_path


def test_collection_category_covers_major_test_subtrees() -> None:
    assert _category_for_test_path(Path("tests/common/test_macro_compile.py")) == "common"
    assert _category_for_test_path(Path("tests/keymasqd/test_daemon_capture_commands.py")) == (
        "keymasqd"
    )
    assert _category_for_test_path(Path("tests/session/test_dbus.py")) == "session"
    assert _category_for_test_path(Path("tests/gui/test_action_labels.py")) == "gui"


def test_collection_category_uses_repo_tests_segment_for_absolute_paths() -> None:
    path = Path("/tmp/tests/work/keymasq/tests/common/test_macro_compile.py")

    assert _category_for_test_path(path) == "common"


def test_collection_category_uses_explicit_root_level_filename_mapping() -> None:
    assert _category_for_test_path(Path("tests/test_capture_manager.py")) == "keymasqd"
    assert _category_for_test_path(Path("tests/test_kde_listener.py")) == "session"
    assert _category_for_test_path(Path("tests/test_devices.py")) == "common"


def test_collection_category_maps_all_current_root_level_test_files() -> None:
    tests_dir = Path(__file__).resolve().parents[1]
    root_test_files = {path.name for path in tests_dir.glob("test_*.py")}

    assert set(_CATEGORY_BY_FILE) == root_test_files


def test_collection_category_uses_top_level_filename_before_ancestor_category() -> None:
    assert (
        _category_for_test_path(Path("/tmp/tests/common/keymasq/tests/test_capture_manager.py"))
        == "keymasqd"
    )
    assert (
        _category_for_test_path(Path("/tmp/tests/keymasqd/keymasq/tests/test_session_clients.py"))
        == "session"
    )
