# Performance: Phase 1 measurements (ADR-0016)

* **Machine:** Windows 11 developer laptop. Git subprocess cost there is about 24 ms per
  call; one object read through `git cat-file --batch` is about 1 ms.
* **Data:** the Release Lab (`release-orchestrator lab-init`), scenario `/library-release
  #15` (FIN-211 needs FIN-198).
* **Tool:** `scripts/bench_phase1.py`. Every step runs in a fresh process, like the Skill,
  and every Git subprocess is counted and timed by the same wrapper for both engine
  versions.
* **Baseline:** commit `b4ba695` (before Phase 1).

## End to end

| Flow | Processes | End-to-end | Engine | Git | Git subprocesses | Output to Claude |
|---|---|---|---|---|---|---|
| Baseline Skill flow (`b4ba695`) | 8 | **73.3 s** | 8.6 s * | 61.6 s | **2 663** | 52.6 KB JSON |
| `assess --pr 15`, cold cache | 1 | **4.8 s** | 1.3 s | 3.1 s | **118** | 12.5 KB JSON, card 0.6 KB |
| `assess --pr 15`, warm cache | 1 | **0.65 s** (3 runs: 0.67 / 0.66 / 0.65) | 0.08 s | 0.26 s | **22** | 12.5 KB JSON, card 0.6 KB |

These are the final measurements, after the code-quality fixes (reader lifecycle, exact
patch-ids, code fingerprint), on a fresh lab. They were taken after the full suite ran, and
no `git cat-file` process survived either run.

\* The baseline "engine" time includes about 3.3 s of GitHub API time in its `context`
step, which that version couldn't report separately. Python start-up and imports add
about 0.36 s to every process; that is included in end-to-end.

**Network/connector time:**

* For `#15`, there is none: 0 network calls, because the default branch isn't needed when
  a PR is given.
* With no PR (`/library-release`, or a ticket only), the remote default branch is looked
  up on GitHub. On the lab that took 3 calls and 0.6–0.7 s.
* `assess` reports `process.network_seconds` separately.

**Claude model time** (reading the card and replying) isn't measurable by the engine, and
isn't included in any number above.

## Per baseline step

| Step (baseline) | End-to-end | Git calls |
|---|---|---|
| context | 4.4 s | 9 |
| resolve-change --pr 15 | 3.2 s | 122 |
| release-graph --ticket FIN-211 | 18.1 s | 739 |
| release-mode discover | 2.9 s | 112 |
| release-mode options | 7.1 s | 289 |
| release-mode preview (Q2 lookup) | 8.0 s | 326 |
| release-mode preview (final) | 7.8 s | 326 |
| release-report | 21.7 s | 740 |

## Targets (V5, section 6)

| Metric | Target | Measured | Met |
|---|---|---|---|
| Engine calls for `#15` | 1 | 1 | ✅ |
| `#15` cold | ≤ 6 s | 4.8 s | ✅ |
| `#15` warm | ≤ 1.5 s | 0.65 s | ✅ |
| Git subprocesses, cold | ≤ 120 | 118 | ✅ (tight) |
| Git subprocesses, warm | ≤ 25 | 22 | ✅ |
| `release-graph` (full), no disk cache | ≤ 5 s / ≤ 150 calls | 4.2 s (memo and batching only); 0.83 s warm | ✅ (calls not re-measured for this command) |
| Default output | ≤ 8 lines | 7–8 lines | ✅ |

**Where the remaining time goes:**

* **Cold:** mostly the cherry-pick probe. It clones the repository and cherry-picks once
  per distinct plan (8 clones and 11 cherry-picks for `#15`). It's cached afterwards.
* **Warm:** live reads of mutable refs only, which are never cached by design:
  * `rev-parse` of branches and tags;
  * `tag --list`;
  * `remote get-url`;
  * `symbolic-ref`;
  * one `cat-file --batch` process per consumer repository.

## Gates in CI

`pytest -m benchmark` asserts the Git subprocess counts (cold ≤ 120, warm ≤ 25),
which are deterministic. On Linux it also asserts wall time at 2× target. Windows times
are printed, not gated.

## Correctness of the speed-ups

`tests/lab/test_phase1_differential.py` compares three runs field by field: original mode
(`LRO_OPTIMIZE=0`, no cache), optimized with a cold cache, and optimized with a warm cache.
**Results are identical.**

**Release Lab:** compared on discovery, scans, patch-ids, 16 plans, Q1 options for all
three modes, and the full and selection graphs.

**Synthetic repository**, covering:

* an undeclared code dependency;
* a cherry-pick conflict;
* a merge commit;
* a multi-commit ticket;
* a binary file;
* a rename;
* ambiguous and unmapped commits;
* an already-released `-x` cherry-pick.

## Test-suite runtime (same machine)

| Run | Result | Time | Notes |
|---|---|---|---|
| Before Phase 1 (`b4ba695`) | 312 passed | ≈ 25 min | |
| Phase 1, before the quality review (baseline) | 328 passed | 7 min 01 s | **leaked `git cat-file` readers**: 80+ idle processes accumulated during the run |
| Phase 1, final | 337 passed | 9 min 13 s | 0 `cat-file` processes alive afterwards |

**Why the final run is slower than the leaky baseline:**

* there are 9 more tests, including about 10 s of reader-lifecycle cycles;
* code that runs outside a CLI session (direct library calls in tests) now uses the
  original `git show` path instead of an unbounded reader per repository.

That is the deliberate trade-off of deterministic cleanup. CLI runs, which is what the
Skill uses, still use the batch reader; the benchmarks above come from them.
