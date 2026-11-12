"""Run state is protected by optimistic concurrency (compare-and-swap on a revision)."""

from __future__ import annotations

import threading

import pytest

from release_orchestrator.errors import ConcurrentModificationError
from release_orchestrator.state import RunStatus, RunStore


def _new(store: RunStore) -> None:
    run, _ = store.create_or_load("abc123", "lib", "1.1.0")
    run.transition(RunStatus.PLAN_READY)
    store.save(run)


def test_stale_copy_cannot_overwrite_a_newer_save(tmp_path):
    store = RunStore(tmp_path)
    _new(store)
    first, second = store.load("abc123"), store.load("abc123")
    first.transition(RunStatus.PREPARING, "process A")
    store.save(first)
    second.transition(RunStatus.APPROVAL_REQUIRED, "process B")
    with pytest.raises(ConcurrentModificationError) as exc:
        store.save(second)
    assert exc.value.details == {"loaded": 1, "stored": 2}
    reloaded = store.load("abc123")
    assert reloaded.state is RunStatus.PREPARING  # process A's work survived
    assert reloaded.revision == 2


def test_concurrent_writers_with_reload_retry_lose_nothing(tmp_path):
    store = RunStore(tmp_path)
    _new(store)
    errors: list[Exception] = []

    def writer(i: int) -> None:
        for _ in range(100):
            run = store.load("abc123")
            run.record(f"writer-{i}", {"i": i})
            try:
                store.save(run)
                return
            except ConcurrentModificationError:
                continue
        errors.append(RuntimeError(f"writer {i} starved"))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    final = store.load("abc123")
    assert not errors
    assert {f"writer-{i}" for i in range(8)} <= set(final.steps)  # no lost update
    assert final.revision == 1 + 8


def test_stale_lock_fails_loudly_instead_of_writing(tmp_path):
    store = RunStore(tmp_path)
    _new(store)
    (tmp_path / "runs" / "abc123.lock").write_text("held by a crashed process")
    run = store.load("abc123")
    run.record("x", 1)
    with pytest.raises(ConcurrentModificationError) as exc:
        store.save(run)
    assert "locked" in exc.value.message
    assert "x" not in store.load("abc123").steps


def test_restart_resumes_from_durable_state(tmp_path):
    _new(RunStore(tmp_path))
    resumed = RunStore(tmp_path).load("abc123")  # a new process
    assert resumed.state is RunStatus.PLAN_READY and resumed.revision == 1
    resumed.transition(RunStatus.PREPARING, "after restart")
    RunStore(tmp_path).save(resumed)
    assert RunStore(tmp_path).load("abc123").history[-1]["note"] == "after restart"


# --------------------------------------------------------------------------- failure modes
def _fail_n(monkeypatch, target, name, times, exc):  # type: ignore[no-untyped-def]
    real = getattr(target, name)
    calls = {"n": 0}

    def flaky(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] <= times:
            raise exc
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, flaky)
    return calls


def test_posix_permission_error_is_raised_not_disguised_as_lock(tmp_path, monkeypatch):
    import os

    from release_orchestrator import state as state_mod

    store = RunStore(tmp_path)
    _new(store)
    run = store.load("abc123")
    monkeypatch.setattr(state_mod, "_WINDOWS", False)
    calls = _fail_n(monkeypatch, os, "open", 10**6, PermissionError("read-only state dir"))
    with pytest.raises(PermissionError):
        store.save(run)
    assert calls["n"] == 1  # not retried on POSIX


def test_persistent_windows_permission_error_without_lock_is_reported_as_itself(
    tmp_path, monkeypatch
):
    import os

    from release_orchestrator import state as state_mod

    store = RunStore(tmp_path)
    _new(store)
    run = store.load("abc123")
    monkeypatch.setattr(state_mod, "_WINDOWS", True)
    _fail_n(monkeypatch, os, "open", 10**6, PermissionError("access denied"))
    with pytest.raises(PermissionError):  # not ConcurrentModificationError
        store.save(run)


def test_transient_sharing_violation_on_replace_is_retried(tmp_path, monkeypatch):
    import os

    from release_orchestrator import state as state_mod

    store = RunStore(tmp_path)
    _new(store)
    run = store.load("abc123")
    run.record("x", 1)
    monkeypatch.setattr(state_mod, "_WINDOWS", True)
    calls = _fail_n(monkeypatch, os, "replace", 3, PermissionError("sharing violation"))
    store.save(run)
    assert calls["n"] == 4 and run.revision == 2
    assert store.load("abc123").steps["x"]["result"] == 1


def test_failed_write_cleans_up_and_leaves_state_untouched(tmp_path, monkeypatch):
    import os

    from release_orchestrator import state as state_mod

    store = RunStore(tmp_path)
    _new(store)
    run = store.load("abc123")
    run.record("x", 1)
    monkeypatch.setattr(state_mod, "_WINDOWS", True)
    _fail_n(monkeypatch, os, "replace", 10**6, PermissionError("still open elsewhere"))
    with pytest.raises(PermissionError):
        store.save(run)
    runs = tmp_path / "runs"
    assert not (runs / "abc123.lock").exists() and not (runs / "abc123.json.tmp").exists()
    assert run.revision == 1  # in-memory revision not advanced by a failed save
    monkeypatch.undo()
    assert "x" not in store.load("abc123").steps
    store.save(run)  # the same copy can still be saved once the obstacle is gone
    assert store.load("abc123").revision == 2


def test_cleanup_failure_never_masks_the_original_error(tmp_path, monkeypatch):
    from pathlib import Path

    store = RunStore(tmp_path)
    _new(store)
    stale = store.load("abc123")
    fresh = store.load("abc123")
    fresh.record("y", 1)
    store.save(fresh)
    _fail_n(monkeypatch, Path, "unlink", 10**6, PermissionError("cannot delete"))
    with pytest.raises(ConcurrentModificationError):  # the real error, not the cleanup one
        store.save(stale)
