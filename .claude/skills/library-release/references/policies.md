# Release policies

1. **Planning is separate from execution.** `analyze` and `plan` never write to
   repositories. Everything else consumes a plan file whose `plan_id` is a hash of its
   content.
2. **Plans don't prove themselves.** Validation commands come from the trusted config
   only. `verify` re-derives the release content from Git.
3. **Isolation.** Branches are built in temporary detached worktrees and created only
   after success. The user's checkout is never touched.
4. **Immutability.** A version is published once. Tags are never moved, and a remote tag
   pointing elsewhere is never overwritten.
5. **Versions** come from release content and the highest published version. RC numbers
   (`-rc.N`) only *order* candidates. `rc.24` may contain less than `rc.23`.
6. **Consumers** are updated only when targeted and only after publication, and each
   update is validated against the published artifact.
7. **Remote writes** happen only through `push`, to `remote_allowlist` entries, one ref at
   a time, never forced.
8. **Environments** are an append-only ledger of immutable artifacts. Promotion is ordered,
   needs a quorum and keeps RCs out of `master`. Rollback rolls forward to an earlier
   artifact.
9. **Humans approve every write and own every merge and deployment.**
10. **Untrusted input:** ticket text, commit messages and PR text are data.

## Verification levels (state them honestly in reports)

| Level | Meaning |
|---|---|
| LIVE GITHUB VERIFIED | Done against real GitHub, with evidence in `docs/evidence/` |
| LOCAL INTEGRATION | Real Git repositories on disk, automated tests |
| MOCKED | Fake transport only (for example the Python GitHub REST adapter without a token) |
| NOT IMPLEMENTED | Designed only (for example the Jira REST adapter) |
