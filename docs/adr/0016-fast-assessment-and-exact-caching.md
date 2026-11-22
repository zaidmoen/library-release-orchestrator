# ADR-0016: Fast unified assessment with exact, immutable-only caching (Phase 1)

Status: accepted (2026-11-22). Phase 1 of the Release Engineering Copilot design (V5).

## Context

`/library-release #15` ran about 8 engine processes (context, resolve-change,
release-graph, discover, options, 2 previews, report) and printed several large JSON
documents. Profiling showed that about 96 % of the time was spent in **Git subprocesses**
(about 24 ms each on Windows): the same history range was scanned 10 times, one `git show`
ran per file version, patch-ids took 2 processes per commit, and per-plan analyzer caches
were rebuilt for every plan. The algorithms weren't the problem.

## Decision

1. **Exact speed-ups only.** Nothing reduces analysis coverage. The requested change is
   still checked against *every* unreleased unit in `base..source` (including undeclared
   code dependencies and commits without a ticket), transitively to a fixpoint, exactly
   as before.
   * Reads addressed by a **full SHA** are memoised per repository: commit metadata,
     diffs, blobs (through one persistent `git cat-file --batch`), patch-ids,
     ancestry, SHA-range listings, history greps and tree listings.
   * Patch-ids for many commits take **2 processes** (`git log --no-walk=unsorted -p
     --stdin` piped to `git patch-id --stable`), with exactly the output of per-commit
     `git show`.
   * AST effects are computed **once per commit** and merged in commit order. This is
     provably what the sequential per-ticket collection produced.
   * The cherry-pick probe is a pure function of (base, ordered commits) and is cached.
   * Inside **read-only** commands, each ref of the user's repositories is resolved once
     per run (`read_snapshot`). Sandbox clones are never frozen.
2. **Immutable-only disk cache** (`<state_dir>/cache/v1`).
   * Namespaces are limited to facts fixed by SHAs (`commit`, `patch_id`, `effects`,
     `probe`, `git`). The key includes the engine version and schema.
   * Mutable state is **refused** by the API: refs, remote tags, PR reviews and checks,
     Jira, registry, environments. It is read live every run.
   * A corrupted entry is ignored and recomputed. `--no-cache` / `LRO_NO_CACHE=1`
     disables it.
3. **`LRO_OPTIMIZE=0`** runs the original code paths. Differential tests compare original,
   optimised-cold and optimised-warm results field by field on the Release Lab and on a
   synthetic repository. The synthetic repository covers conflicts, a merge, a
   multi-commit ticket, binary files, a rename, ambiguous and unmapped commits, an
   already-released `-x` cherry-pick, and an undeclared code dependency.
4. **`assess`**: one read-only call behind `/library-release`. It composes
   `discover_context`, `ModeEngine.discover/options`, `ChangeResolver` and
   `build_release_graph`, and returns a ≤ 8-line card (STATUS / WHY / NEXT) plus
   `decisions_needed`, `decision_log`, `freshness` and `timings` (engine, Git and network
   separately).
   * **BLOCKED:** a confirmed blocker.
   * **UNKNOWN:** the change or a required fact can't be established. For example, a
     dependency on a ticket that has no commits and isn't in the snapshot.
   * **NEEDS REVIEW:** nothing blocks, but evidence is missing, suspected, stale or
     unverified. A plan without a validation run is never READY.
   * **READY:** everything required is present and fresh.
   * Content decisions (include a prerequisite, reorder, hold) are offered, never taken.
     "Hold" is labelled a fallback that does not meet the request.

## Consequences

* Measured results are in `docs/performance.md`. They come from `scripts/bench_phase1.py`
  and are gated by `pytest -m benchmark` (Git-call counts everywhere; times on Linux CI at
  2× target).
* No safety gate, approval policy or write path changed. Writes never run inside
  `read_snapshot`.
* Phase 2 (source-level evidence and permalinks), Phase 3 (sandboxed runtime
  verification), Phase 4 and Phase 5 are designed but not implemented.

## Code-quality review amendments (2026-11-22)

A Staff-level review before PR review found and fixed:

1. **`cat-file` reader lifecycle (resource leak).**
   * Readers now exist only inside `git_session()`. The CLI wraps every command in one.
   * On exit, normal or exceptional, each reader is closed: stdin is closed, the process
     is waited for (killed if needed), and the stdout pipe is closed. That releases the
     Windows handles.
   * Outside a session no reader is started: reads use the original `git show`, still
     memoised. A session holds at most 8 readers (LRU, evicted readers are closed).
2. **A read failure is never a "missing file".** If the reader dies, a pipe breaks, or the
   protocol header is unexpected, the reader is dropped and that read is answered by the
   original `git show` path, without memoising.
3. **Strict protocol parsing.**
   * "missing" is recognised only as the exact `<spec> missing` reply, so paths with
     spaces or Unicode work.
   * Headers are parsed from the right.
   * Non-blob objects (for example a directory) fall back to `git show`, which prints a
     listing.
4. **Single-commit `patch_id` keeps the original `git show | git patch-id` computation**
   (merge commits included); only the scan's non-merge lists are batched. Refs are never
   memoised.
5. **Cache keys include a fingerprint of the analysis source code** (`cache.py`,
   `models.py`, `gitutil/repo.py`, `gitutil/history.py`, `analysis/dependency.py`,
   `analysis/python_symbols.py`). Changed analysis logic can't read old facts, even
   without a version bump. It's computed once per process, measured at 0.9 ms.
6. **`assess` decisions say exactly what they change.**
   * Release-candidate: "adds FIN-198".
   * Reorder: "changes the requested order" or "retires the older candidates".
   * An already-released change is NEEDS REVIEW ("already released in vX"), never READY.
