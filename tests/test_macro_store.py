from pathlib import Path

import pytest

from keyforge.keyforged.macro_store import MacroStore


def test_macro_store_crud_and_revision(tmp_path: Path) -> None:
    store = MacroStore(tmp_path / "macros")

    created = store.create(
        {
            "name": "combo",
            "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
            "duration_ms": 1,
            "device_types": ["keyboard"],
        }
    )
    assert created["name"] == "combo"
    assert created["revision"] == 1

    updated = store.update("combo", {"duration_ms": 20}, expected_revision=1)
    assert updated["duration_ms"] == 20
    assert updated["revision"] == 2

    renamed = store.rename("combo", "combo_new", expected_revision=2)
    assert renamed["name"] == "combo_new"
    assert renamed["revision"] == 3

    metas = store.list_meta()
    assert [m["name"] for m in metas] == ["combo_new"]

    store.delete("combo_new", expected_revision=3)
    assert store.list_meta() == []


def test_macro_store_revision_conflict(tmp_path: Path) -> None:
    store = MacroStore(tmp_path / "macros")
    store.create({"name": "macro_a", "events": []})

    with pytest.raises(ValueError):
        store.update("macro_a", {"duration_ms": 10}, expected_revision=7)
