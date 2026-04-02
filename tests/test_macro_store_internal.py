import tempfile
from pathlib import Path

import pytest

from keyforge.keyforged.macro_store import MacroStore


def test_register_internal_macro():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = MacroStore(Path(tmpdir))
        store.register_internal(
            "__test_macro",
            events=[{"type": "key", "code": "a"}],
            duration_ms=100,
        )

        assert store.is_internal("__test_macro")
        macro = store.get("__test_macro")
        assert macro["name"] == "__test_macro"
        assert macro["internal"] is True
        assert len(macro["events"]) == 1


def test_internal_macro_not_listed():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = MacroStore(Path(tmpdir))
        store.ensure()

        store.register_internal("__hidden", events=[])

        macros = store.list_meta()
        names = [m["name"] for m in macros]
        assert "__hidden" not in names


def test_cannot_create_macro_with_internal_prefix():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = MacroStore(Path(tmpdir))
        store.ensure()

        with pytest.raises(ValueError, match="reserved"):
            store.create({"name": "__my_macro", "events": []})


def test_cannot_update_internal_macro():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = MacroStore(Path(tmpdir))
        store.register_internal("__protected", events=[])

        with pytest.raises(PermissionError, match="internal macro"):
            store.update("__protected", {"events": []}, None)


def test_cannot_rename_internal_macro():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = MacroStore(Path(tmpdir))
        store.register_internal("__protected", events=[])

        with pytest.raises(PermissionError, match="internal macro"):
            store.rename("__protected", "new_name", None)


def test_cannot_delete_internal_macro():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = MacroStore(Path(tmpdir))
        store.register_internal("__protected", events=[])

        with pytest.raises(PermissionError, match="internal macro"):
            store.delete("__protected", None)


def test_cannot_rename_to_internal_prefix():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = MacroStore(Path(tmpdir))
        store.ensure()

        store.create({"name": "normal_macro", "events": []})

        with pytest.raises(ValueError, match="reserved"):
            store.rename("normal_macro", "__internal_name", None)


def test_get_returns_copy_of_internal_macro():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = MacroStore(Path(tmpdir))
        store.register_internal("__test", events=[{"type": "key"}])

        macro1 = store.get("__test")
        macro2 = store.get("__test")

        assert macro1 is not macro2
