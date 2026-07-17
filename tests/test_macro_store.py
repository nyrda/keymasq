import logging
import lzma
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from pathlib import Path

import pytest

from keymasq.keymasqd import macro_store as macro_store_module
from keymasq.keymasqd.macro_file import MacroFileMeta
from keymasq.keymasqd.macro_store import MacroStore


def _run_before_mutation_guard(
    store: MacroStore,
    monkeypatch: pytest.MonkeyPatch,
    callback: Callable[[], None],
) -> None:
    original_guard = store._mutation_guard
    callback_ran = False

    @contextmanager
    def mutation_guard() -> Generator[None, None, None]:
        nonlocal callback_ran
        if not callback_ran:
            callback_ran = True
            callback()
        with original_guard():
            yield

    monkeypatch.setattr(store, "_mutation_guard", mutation_guard)


def test_macro_store_crud_and_revision(tmp_path: Path) -> None:
    store = MacroStore(tmp_path / "macros")

    created = store.create(
        {
            "name": "combo",
            "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
            "duration_us": 1000,
            "device_types": ["keyboard"],
        }
    )
    assert created["name"] == "combo"
    assert created["revision"] == 1
    assert not (tmp_path / "macros" / "combo.json").exists()
    assert (tmp_path / "macros" / "combo.kmacro.xz").exists()
    assert list(store.iter_events("combo")) == [{"type": 1, "code": 30, "value": 1, "t_us": 0}]

    updated = store.update("combo", {"duration_us": 20_000}, expected_revision=1)
    assert updated["duration_us"] == 20_000
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
        store.update("macro_a", {"duration_us": 10_000}, expected_revision=7)


def test_macro_store_snapshot_pins_metadata_and_repeated_events_to_one_revision(
    tmp_path: Path,
) -> None:
    store = MacroStore(tmp_path / "macros")
    old_event = {"type": 1, "code": 30, "value": 1, "t_us": 0}
    new_event = {"type": 1, "code": 31, "value": 1, "t_us": 100}
    store.create({"name": "macro", "events": [old_event]})

    snapshot = store.open_snapshot("macro")
    store.update("macro", {"events": [new_event]}, expected_revision=1)
    assert snapshot.meta["revision"] == 1
    assert list(snapshot.iter_events()) == [old_event]
    assert list(snapshot.iter_events()) == [old_event]

    assert store.get("macro")["revision"] == 2
    assert list(store.iter_events("macro")) == [new_event]


def test_macro_store_update_rechecks_revision_under_mutation_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MacroStore(tmp_path / "macros")
    contender = MacroStore(tmp_path / "macros")
    store.create({"name": "macro_a", "events": [], "duration_us": 0})

    _run_before_mutation_guard(
        store,
        monkeypatch,
        lambda: contender.update("macro_a", {"duration_us": 50_000}, expected_revision=1),
    )

    with pytest.raises(ValueError, match="Revision conflict"):
        store.update("macro_a", {"duration_us": 10_000}, expected_revision=1)

    current = store.get("macro_a")
    assert current["duration_us"] == 50_000
    assert current["revision"] == 2


def test_macro_store_rename_rechecks_revision_under_mutation_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MacroStore(tmp_path / "macros")
    contender = MacroStore(tmp_path / "macros")
    store.create({"name": "macro_a", "events": [], "duration_us": 0})

    _run_before_mutation_guard(
        store,
        monkeypatch,
        lambda: contender.update("macro_a", {"duration_us": 50_000}, expected_revision=1),
    )

    with pytest.raises(ValueError, match="Revision conflict"):
        store.rename("macro_a", "macro_b", expected_revision=1)

    current = store.get("macro_a")
    assert current["duration_us"] == 50_000
    assert current["revision"] == 2
    assert not (tmp_path / "macros" / "macro_b.kmacro.xz").exists()


def test_macro_store_delete_rechecks_revision_under_mutation_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MacroStore(tmp_path / "macros")
    contender = MacroStore(tmp_path / "macros")
    store.create({"name": "macro_a", "events": [], "duration_us": 0})

    _run_before_mutation_guard(
        store,
        monkeypatch,
        lambda: contender.update("macro_a", {"duration_us": 50_000}, expected_revision=1),
    )

    with pytest.raises(ValueError, match="Revision conflict"):
        store.delete("macro_a", expected_revision=1)

    current = store.get("macro_a")
    assert current["duration_us"] == 50_000
    assert current["revision"] == 2


def test_macro_store_rename_keeps_source_when_destination_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MacroStore(tmp_path / "macros")
    store.create(
        {
            "name": "macro_a",
            "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        }
    )

    def write_corrupt_destination(
        path: Path,
        _data: dict[str, object],
        *,
        overwrite: bool = True,
    ) -> None:
        _ = overwrite
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not xz", encoding="utf-8")

    monkeypatch.setattr(store, "_write_payload", write_corrupt_destination)

    with pytest.raises(lzma.LZMAError):
        store.rename("macro_a", "macro_b", expected_revision=1)

    assert store.get("macro_a")["name"] == "macro_a"
    assert not (tmp_path / "macros" / "macro_b.kmacro.xz").exists()


def test_macro_store_create_preserves_destination_created_after_precheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MacroStore(tmp_path / "macros")
    original_write_macro = macro_store_module.write_macro
    existing_event = {"type": 1, "code": 99, "value": 1, "t_us": 0}

    def write_after_race(
        path: Path,
        meta: MacroFileMeta,
        events: Iterable[dict[str, object]],
        *,
        overwrite: bool = True,
    ) -> None:
        if meta.name == "race" and not path.exists():
            original_write_macro(
                path,
                MacroFileMeta(name="race", event_count=1),
                [existing_event],
            )
        original_write_macro(path, meta, events, overwrite=overwrite)

    monkeypatch.setattr(macro_store_module, "write_macro", write_after_race)

    with pytest.raises(FileExistsError):
        store.create(
            {
                "name": "race",
                "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
            }
        )

    assert store.get("race")["events"] == [existing_event]


def test_macro_store_rename_preserves_destination_created_after_precheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MacroStore(tmp_path / "macros")
    store.create(
        {
            "name": "macro_a",
            "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        }
    )
    original_write_macro = macro_store_module.write_macro
    existing_event = {"type": 1, "code": 99, "value": 1, "t_us": 0}

    def write_after_race(
        path: Path,
        meta: MacroFileMeta,
        events: Iterable[dict[str, object]],
        *,
        overwrite: bool = True,
    ) -> None:
        if meta.name == "macro_b" and not path.exists():
            original_write_macro(
                path,
                MacroFileMeta(name="macro_b", event_count=1),
                [existing_event],
            )
        original_write_macro(path, meta, events, overwrite=overwrite)

    monkeypatch.setattr(macro_store_module, "write_macro", write_after_race)

    with pytest.raises(FileExistsError):
        store.rename("macro_a", "macro_b", expected_revision=1)

    assert store.get("macro_a")["name"] == "macro_a"
    assert store.get("macro_b")["events"] == [existing_event]


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
            "duration_us": 1000,
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


def test_macro_store_create_from_events_requires_streamed_metadata(tmp_path: Path) -> None:
    store = MacroStore(tmp_path / "macros")
    events = iter(
        [
            {"device_type": "keyboard", "type": 1, "code": 30, "value": 1, "t_us": 5000},
        ]
    )

    with pytest.raises(ValueError, match="event_count, duration_us, device_types"):
        store.create_from_events({"name": "streamed"}, events, return_full=False)

    assert not (tmp_path / "macros" / "streamed.kmacro.xz").exists()


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
    assert loaded["duration_us"] == 2000
    assert updated["events"] == events
    assert updated["duration_us"] == 2000


def test_macro_store_recomputes_meta_when_replacing_explicit_events(tmp_path: Path) -> None:
    store = MacroStore(tmp_path / "macros")
    replacement_events = [
        {"device_type": "mouse", "type": 2, "code": 0, "value": 5, "t_us": 5000},
    ]

    store.create(
        {
            "name": "timed",
            "events": [
                {"device_type": "keyboard", "type": 1, "code": 30, "value": 1, "t_us": 1000},
            ],
            "duration_us": 1000,
            "device_types": ["keyboard"],
        }
    )
    updated = store.update("timed", {"events": replacement_events}, expected_revision=1)
    meta = store.get_meta("timed")
    list_meta = store.list_meta()

    assert updated["duration_us"] == 5000
    assert updated["device_types"] == ["mouse"]
    assert meta["duration_us"] == 5000
    assert meta["device_types"] == ["mouse"]
    assert list_meta[0]["duration_us"] == 5000
    assert list_meta[0]["device_types"] == ["mouse"]


def test_macro_store_internal_meta_uses_macro_file_meta_payload(tmp_path: Path) -> None:
    store = MacroStore(tmp_path / "macros")
    store.register_internal(
        "__internal",
        [{"device_type": "keyboard", "type": 1, "code": 30, "value": 1, "t_us": 0}],
        created_at="2026-05-31T20:00:00",
        loop_count="2",
        loop_mode="",
    )

    meta = store.get_meta("__internal")

    assert meta == MacroFileMeta.from_payload(
        store.get("__internal"),
        name="__internal",
    ).to_payload()


def test_macro_store_list_meta_redacts_type_macro_text(tmp_path: Path) -> None:
    store = MacroStore(tmp_path / "macros")
    store.create(
        {
            "name": "type_secret",
            "events": [{"device_type": "keyboard", "type": 1, "code": 30, "value": 1, "t_us": 0}],
            "type_binding": True,
            "type_text": "hunter2",
            "type_down_ms": 20,
            "type_pause_ms": 30,
            "type_use_unicode_input": True,
        }
    )

    listed = store.list_meta()[0]
    loaded = store.get("type_secret")

    assert listed["type_binding"] is True
    assert "type_text" not in listed
    assert listed["type_down_ms"] == 20
    assert listed["type_pause_ms"] == 30
    assert listed["type_use_unicode_input"] is True
    assert loaded["type_text"] == "hunter2"


def test_macro_store_update_can_clear_type_metadata(tmp_path: Path) -> None:
    store = MacroStore(tmp_path / "macros")
    store.create(
        {
            "name": "type_macro",
            "events": [{"device_type": "keyboard", "type": 1, "code": 30, "value": 1, "t_us": 0}],
            "type_binding": True,
            "type_text": "secret",
            "type_down_ms": 20,
            "type_pause_ms": 30,
            "type_use_unicode_input": True,
        }
    )

    updated = store.update(
        "type_macro",
        {
            "events": [{"device_type": "keyboard", "type": 1, "code": 31, "value": 1, "t_us": 0}],
            "type_binding": False,
        },
        expected_revision=1,
    )

    assert "type_binding" not in updated
    assert "type_text" not in updated
    assert "type_down_ms" not in updated
    assert "type_pause_ms" not in updated
    assert "type_use_unicode_input" not in updated
    assert "type_text" not in store.get("type_macro")


def test_macro_store_update_replacing_events_clears_stale_type_metadata(tmp_path: Path) -> None:
    store = MacroStore(tmp_path / "macros")
    store.create(
        {
            "name": "type_macro",
            "events": [{"device_type": "keyboard", "type": 1, "code": 30, "value": 1, "t_us": 0}],
            "type_binding": True,
            "type_text": "secret",
            "type_down_ms": 20,
            "type_pause_ms": 30,
            "type_use_unicode_input": True,
        }
    )

    updated = store.update(
        "type_macro",
        {"events": [{"device_type": "keyboard", "type": 1, "code": 31, "value": 1, "t_us": 0}]},
        expected_revision=1,
    )

    persisted = store.get("type_macro")

    assert "type_binding" not in updated
    assert "type_text" not in updated
    assert "type_down_ms" not in updated
    assert "type_pause_ms" not in updated
    assert "type_use_unicode_input" not in updated
    assert "type_binding" not in persisted
    assert "type_text" not in persisted
    assert "type_down_ms" not in persisted
    assert "type_pause_ms" not in persisted
    assert "type_use_unicode_input" not in persisted


def test_macro_store_update_replacing_events_preserves_fresh_type_metadata(tmp_path: Path) -> None:
    store = MacroStore(tmp_path / "macros")
    store.create(
        {
            "name": "type_macro",
            "events": [{"device_type": "keyboard", "type": 1, "code": 30, "value": 1, "t_us": 0}],
            "type_binding": True,
            "type_text": "old",
        }
    )

    updated = store.update(
        "type_macro",
        {
            "events": [{"device_type": "keyboard", "type": 1, "code": 31, "value": 1, "t_us": 0}],
            "type_binding": True,
            "type_text": "new",
            "type_down_ms": 5,
            "type_pause_ms": 10,
        },
        expected_revision=1,
    )

    assert updated["type_binding"] is True
    assert updated["type_text"] == "new"
    assert updated["type_down_ms"] == 5
    assert updated["type_pause_ms"] == 10


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

    assert "Skipping corrupt compressed macro file" in caplog.text
    assert "broken.kmacro.xz" in caplog.text
