# Production-readiness assessment

**Verdict: not production-ready.** The decision engine is solid and well tested locally.
The integrations and governance needed for production adoption are incomplete.

## Capability levels

| Capability | Level |
|---|---|
| Dependency decision (code dependency, conflict, behavioural warning), RC subsets | LOCAL INTEGRATION (S02–S06) |
| Isolated release branch, verification against plan, validation gate | LOCAL INTEGRATION (S01, S05, S08) |
| Immutable publish, idempotency, partial-failure recovery | LOCAL INTEGRATION (S09, S12), local registry only |
| Consumer targeting, breaking-API protection | LOCAL INTEGRATION (S07, S11) |
| Allowlisted, never-forced push | LOCAL INTEGRATION against local bare remotes (S09, S15) |
| Environment ledger, promotion and rollback | LOCAL INTEGRATION (S13, S14) |
| Draft PRs on GitHub via MCP + `record-pr` | See [evidence/live-github.md](evidence/live-github.md). Not verified until that file says so |
| Python GitHub REST adapter | MOCKED |
| Jira REST, remote package registries | NOT IMPLEMENTED |

## Blocking gaps before internal adoption

1. **Live integration evidence.** GitHub PR flow end to end on the sandbox, then a
   read-only pilot on one real library.
2. **Real registry adapter** (Artifactory, CodeArtifact or private PyPI) with
   create-if-absent semantics. The local JSON registry and ledger have single-machine
   atomicity only.
3. **Authenticated approvals.** Approver names are self-declared. Promotion and
   publication must take approvals from PR reviews or a deploy system.
4. **Sandboxed validation.** Tests run with the operator's privileges. Run them in CI or
   containers.
5. **Jira REST adapter** with a read-only service account.
6. **Lockfile ecosystems** (Poetry, uv, pip-tools) and symbol-level consumer impact.
7. **Shared state.** Run state and the ledger live on one machine. Production needs a
   database or a reviewed config repository.

## Known limits that remain by design

* Static dependency analysis is a Python-only heuristic. Behavioural dependencies rely on
  tests (S05 shows both sides).
* The engine never merges, deploys or force-pushes. Those stay with humans and
  deployment tooling.
