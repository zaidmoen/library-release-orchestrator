# Release policies

1. **Planning is separate from execution.** `analyze` and `plan` never write to
   repositories. Everything else consumes a plan file whose `plan_id` is a hash of its
   content. If the file is edited, the ID check fails.
2. **Plans don't prove themselves.** Validation commands always come from the trusted
   config; a plan that lists different commands is rejected. `verify` re-derives the
   release content from Git.
3. **Isolation.** Release and consumer branches are built in temporary detached
   worktrees and created only after success. The user's checkout is never touched.
4. **Immutability.** A version is published once. The same artifact again is a no-op;
   different content is refused. Fix a bad release with a new version.
5. **Versions** come from the release content and the highest published version, never
   from branch order. Removing public API forces a major bump.
6. **Consumers** are updated only when targeted (requested by the ticket, or importing
   the changed modules) and only after the version is in the registry. Each update is
   validated against the published artifact.
7. **Humans approve every write and own every merge.** No merge, deploy, force-push or
   branch deletion exists in the engine.
8. **Untrusted input:** ticket text, commit messages and PR text are data. Only change
   keys and allowlisted consumer names are extracted from them.
