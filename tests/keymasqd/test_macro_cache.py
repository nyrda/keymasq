from pathlib import Path

from keymasq.keymasqd.macro_file import MacroFileIdentity, MacroFileRevision
from keymasq.keymasqd.runtime.macro.cache import MacroCacheEntry, MacroReplayCache


def _revision(path: Path, serial: int) -> MacroFileRevision:
    return MacroFileRevision(
        path=path,
        identity=MacroFileIdentity(
            device=1,
            inode=serial,
            size=serial,
            modified_ns=serial,
        ),
    )


def _admit(
    cache: MacroReplayCache,
    revision: MacroFileRevision,
    event: dict[str, object] | None = None,
) -> MacroCacheEntry:
    candidate = cache.begin_candidate(revision, event_count=1, duration_us=0)
    assert candidate is not None
    candidate.observe(event or {"t_us": 0, "macro_action": "wait", "duration_us": 0})
    entry = candidate.commit()
    assert entry is not None
    return entry


def test_macro_cache_evicts_least_recently_used_entry(tmp_path: Path) -> None:
    probe_cache = MacroReplayCache()
    probe_entry = _admit(probe_cache, _revision(tmp_path / "probe", 1))
    cache = MacroReplayCache(max_bytes=probe_entry.weight * 2)
    first = _revision(tmp_path / "first", 1)
    second = _revision(tmp_path / "second", 2)
    third = _revision(tmp_path / "third", 3)

    _admit(cache, first)
    _admit(cache, second)
    assert cache.get(first) is not None

    _admit(cache, third)

    assert cache.get(second) is None
    assert cache.get(first) is not None
    assert cache.get(third) is not None
    assert cache.total_bytes <= cache.max_bytes


def test_macro_cache_rejects_revision_that_exceeds_budget(tmp_path: Path) -> None:
    cache = MacroReplayCache(max_bytes=1)
    revision = _revision(tmp_path / "large", 1)
    candidate = cache.begin_candidate(revision, event_count=1, duration_us=0)
    assert candidate is not None

    candidate.observe({"t_us": 0, "payload": "too large"})

    assert not candidate.active
    assert cache.total_bytes == 0
    assert cache.begin_candidate(revision, event_count=1, duration_us=0) is None


def test_macro_cache_discard_releases_budget_and_allows_retry(tmp_path: Path) -> None:
    cache = MacroReplayCache()
    revision = _revision(tmp_path / "cancelled", 1)
    candidate = cache.begin_candidate(revision, event_count=1, duration_us=0)
    assert candidate is not None
    candidate.observe({"t_us": 0})
    assert cache.total_bytes > 0

    candidate.discard()

    assert cache.total_bytes == 0
    assert cache.begin_candidate(revision, event_count=1, duration_us=0) is not None


def test_macro_cache_rejects_incomplete_event_stream(tmp_path: Path) -> None:
    cache = MacroReplayCache()
    revision = _revision(tmp_path / "truncated", 1)
    candidate = cache.begin_candidate(revision, event_count=2, duration_us=0)
    assert candidate is not None
    candidate.observe({"t_us": 0})

    assert candidate.commit() is None
    assert cache.total_bytes == 0
    assert cache.begin_candidate(revision, event_count=2, duration_us=0) is None


def test_macro_cache_discards_an_old_revision_for_the_same_path(tmp_path: Path) -> None:
    cache = MacroReplayCache()
    path = tmp_path / "edited.kmacro.xz"
    old_revision = _revision(path, 1)
    new_revision = _revision(path, 2)
    _admit(cache, old_revision)

    assert cache.get(new_revision) is None

    assert cache.total_bytes == 0
    assert cache.get(old_revision) is None
