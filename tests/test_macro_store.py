import logging
from pathlib import Path

import pytest

from keymasq.common.models import DEFAULT_MACRO_LOOP_STOP_BEHAVIOR
from keymasq.keymasqd.macro_store import MacroStore


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
    assert not (tmp_path / "macros" / "combo.json").exists()
    assert (tmp_path / "macros" / "combo.kmacro.xz").exists()
    assert list(store.iter_events("combo")) == [{"type": 1, "code": 30, "value": 1, "t_us": 0}]

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


def test_macro_store_create_from_events_returns_metadata_without_loading_full_payload(
    tmp_path: Path,
) -> None:
    store = MacroStore(tmp_path / "macros")
    events = (
        {"device_type": "keyboard", "type": 1, "code": code, "value": 1, "t_us": code}
        for code in range(3)
    )

    created = store.create_from_events(
        {
            "name": "streamed",
            "duration_ms": 1,
            "device_types": ["keyboard"],
            "event_count": 3,
        },
        events,
        return_full=False,
    )

    assert created["name"] == "streamed"
    assert created["event_count"] == 3
    assert "events" not in created
    assert [event["code"] for event in store.iter_events("streamed")] == [0, 1, 2]


def test_macro_store_preserves_wait_controls(tmp_path: Path) -> None:
    store = MacroStore(tmp_path / "macros")
    events = [
        {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": 1000,
            "macro_action": "wait",
            "duration_us": 50_000,
        },
        {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": 2000,
            "macro_action": "wait_random",
            "min_us": 10_000,
            "max_us": 80_000,
        },
    ]

    store.create({"name": "timed", "events": events[:1]})
    updated = store.update(
        "timed",
        {"events": events},
        expected_revision=1,
    )
    loaded = store.get("timed")

    assert loaded["events"] == events
    assert updated["events"] == events


def test_macro_store_internal_meta_uses_shared_loop_stop_default(tmp_path: Path) -> None:
    store = MacroStore(tmp_path / "macros")
    store.register_internal("__internal", [{"type": 1, "code": 30, "value": 1, "t_us": 0}])

    meta = store.get_meta("__internal")

    assert meta["loop_stop_behavior"] == DEFAULT_MACRO_LOOP_STOP_BEHAVIOR


def test_macro_store_list_meta_logs_unreadable_files(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    macro_dir = tmp_path / "macros"
    macro_dir.mkdir()
    (macro_dir / "broken.kmacro.xz").write_text("not xz")
    store = MacroStore(macro_dir)

    with caplog.at_level(logging.WARNING, logger="keymasqd.macros"):
        assert store.list_meta() == []

    assert "Skipping unreadable macro file" in caplog.text
    assert "broken.kmacro.xz" in caplog.text
