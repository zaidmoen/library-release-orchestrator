# Handing `/library-release` to the team (ADR-0018)

## What an engineer receives

One file: [`distribution/library-release/SKILL.md`](../distribution/library-release/SKILL.md).

```text
~/.claude/skills/library-release/SKILL.md      (Windows: %USERPROFILE%\.claude\skills\...)
```

Then they open any library repository in Claude Code and type `/library-release`.
Nothing else needs to be cloned, configured or installed by hand.

**Prerequisites:**
* Python 3.11+;
* git;
* bash (Claude Code's shell: Git Bash on Windows);
* network access to github.com **once** per engine revision.

## What happens on the first invocation

1. Claude reads the Skill and asks once: *"Install the release engine revision
   `<sha12>` … into `~/.lro`?"*
2. On yes, the `lro-bootstrap` block runs:
   1. detects Python 3.11+ and git;
   2. fetches **exactly** the pinned commit into `~/.lro/tmp/fetch.*` and checks
      `HEAD == <sha>`;
   3. runs that verified checkout's `scripts/lro_engine.py install`. The installer locks,
      re-verifies, moves the checkout to `~/.lro/engines/<sha>/src`, creates `venv/` (only
      PyYAML), writes `bin/lro`, and writes `engine.json` last.
3. The Skill then runs `LRO release-mode discover` in the engineer's repository. This is
   read-only.

Nothing is executed before the SHA check. No repository content, credential or
environment variable is sent to the orchestrator repository: the only network use is
`git fetch` of a public commit.

**Later invocations** only run `lro_engine.py verify`, offline. It checks the cached
checkout's `HEAD`, its tree SHA, that it's clean, and that its origin is allowed. If any
check fails, the Skill stops with `LRO_ERROR=ENGINE_VERIFY_FAILED`.

## Pinning and updating the engine

* The pin is the 40-hex `LRO_SHA` in `SKILL.md`. It appears three times: in the
  bootstrap, in the `LRO` path, and in the reference path.
* To update:
  1. merge the engine change;
  2. pick its commit SHA;
  3. update `distribution/library-release/SKILL.md` (all three occurrences);
  4. review it as a normal PR;
  5. redistribute the file.
* Old revisions stay cached side by side. Remove one with `rm -rf ~/.lro/engines/<sha>`.
* There is no auto-update, and a branch name is rejected.

## Offline vs live

| Works offline (engine cached) | Needs live integrations |
|---|---|
| Discovery, Q1 options, previews, reorder `progress`, impact on local clones, release notes draft, audit report | GitHub MCP: PRs, branches, remote manifests, CI/review status |
| Prepare / validate / publish to the **local** registry | Jira MCP: live ticket status and links (otherwise fixture/snapshot; stale evidence is reported) |
| Operation IDs, events, Slack *message planning* | Slack MCP: posting, channel resolution, identity |
| | First engine install; `push` to an allowlisted remote; draft GitHub Release (`GITHUB_TOKEN`) |

## Permissions the team needs

| Integration | Read | Write (only after an explicit yes) |
|---|---|---|
| GitHub MCP | repository, branches, tags, PRs, files, checks | draft PRs (library and consumers) |
| Jira MCP | issues, links, fix versions | none: Jira is never updated |
| Slack MCP | `slack_search_channels`, `slack_read_user_profile`, `slack_read_thread` / `read_channel` | `slack_send_message` to `#library-release-orchestrator` only. The Slack app must be a member of the (private) channel. |
| `GITHUB_TOKEN` | optional REST fallback for private repositories | draft GitHub Release only (*Contents: read/write* on the library) |

Organisation prerequisites:
* Approve automatic Slack notifications once:
  `LRO notify policy --enable --from <search> --approve`.
* Agree the allowlisted push remotes (`remote_allowlist`) in a reviewed YAML if a team
  needs pushes.

## Read-only demonstration (changes nothing)

```bash
release-orchestrator lab-init --dest ./demo   # local fixture repositories
cd demo/harri-release-lab-library
LRO release-mode discover
LRO release-mode options --mode reorder-releases --order v1.1.0-rc.23,v1.1.0-rc.22   # blocked: FIN-198
LRO release-mode options --mode reorder-releases --order <independent order>          # new candidates per step
LRO release-mode preview --mode reorder-releases --order … --choice requested --out pv.json
LRO release-mode progress --preview pv.json
LRO op start --kind reorder --order …   # local audit only
LRO op report --op <id> --out audit.md
```

Every command above is read-only, apart from local `op` files under the state directory.
No `--approve` is used, nothing is pushed, and Slack is only *planned* (`notify plan`)
unless the policy is enabled.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `LRO_ERROR=PYTHON_MISSING` | install Python 3.11+; on Windows `py -3` is accepted |
| `LRO_ERROR=GIT_MISSING` | install git and make sure it's on `PATH` |
| `LRO_ERROR=FETCH_OR_VERIFY_FAILED` | no network or no access to github.com, or the pinned SHA isn't on the remote (unpushed) |
| `LRO_ERROR=ENGINE_VERIFY_FAILED` | the cached engine was modified; delete `~/.lro/engines/<sha>` and re-run |
| `LOCK_TIMEOUT` | another installation is running; stale locks (> 15 min) are broken automatically |
| `CONCURRENT_OPERATION` | another engineer's operation is active for this library; finish it or record `OPERATION_CANCELLED` |
| `notify plan` says `reconcile` | an earlier Slack post is uncertain; read the thread history and run `notify reconcile` |
| `release-mode progress` says `DRIFT` | the repository changed between steps; re-plan from the current state |

## Acceptance scenarios (A–Z) and how each is verified

**Verification levels:**
* **mocked**: MCP payloads are synthetic;
* **sandbox**: real Git, bash, venv and a local registry in temporary directories;
* **live read-only**: real MCP reads from this session;
* **company**: not done.

| | Scenario | Test | Level |
|---|---|---|---|
| A | first invocation, no engine | `test_engine_distribution::test_a_b_e_*` | sandbox: real bash, `git fetch` by SHA, venv; local fixture engine instead of GitHub |
| B | cached reuse, offline | same test (source deleted) | sandbox |
| C | wrong / unapproved revision, tampering | `test_c_*` (3 tests) | sandbox |
| D | Windows/Linux/macOS paths | `test_d_platform_paths`, Windows run of A–C | sandbox (Windows here; Linux in CI) |
| E | working directory preserved | `test_a_b_e_*` | sandbox |
| F | independent cumulative reorder | `test_enterprise_reorder::test_f_*` | sandbox |
| G | confirmed prerequisite blocks | `test_mcp_native::test_j_*`, `test_release_modes::test_reorder_rc23_before_rc22_*` | sandbox |
| H | three or more candidates | `test_h_j_*`, `test_mcp_native::test_i_three_*` | sandbox |
| I | full multi-step lifecycle | `test_i_u_full_reordered_lifecycle_*` | sandbox (local registry) |
| J | collisions, immutable tags | `test_h_j_*`, `test_mcp_native::test_i_*` | sandbox |
| K | missing/conflicting Jira | `test_issue_eligibility` (inaccessible, stale, disagreement), `test_live_jira_*` | mocked |
| L | missing consumer repositories | `test_l_consumer_classification_*` | sandbox |
| M | GitHub MCP unavailable | `test_mcp_native::test_k_github_unavailable_*` | mocked |
| N | Jira MCP unavailable | `test_issue_eligibility::test_inaccessible_issue_blocks`, `test_stale_or_undated_snapshot_blocks` | mocked |
| O | Slack channel resolution | `test_operations_notify::test_o_*` | mocked (shape from a live read-only search) |
| P | parent + thread replies | `test_p_q_*` | mocked |
| Q | no duplicate notifications | `test_p_q_*`, `test_q_*` | mocked |
| R | Slack failure during a release | `test_r_*` | mocked |
| S | unverified identity | `test_s_*` | mocked (shape from a live read-only profile read) |
| T | stale preview, changed state | `test_t_*` (2), `test_release_modes::test_stale_preview_is_rejected` | sandbox |
| U | partial execution recovery | `test_i_u_*`, `test_run_state_concurrency` | sandbox |
| V | unauthorised write / claim | `test_v_*`, `test_publish_authorization`, approval tests | sandbox + mocked |
| W | concurrent operations | `test_w_*` (install and operations), `test_run_state_concurrency` | sandbox |
| X | divergent candidates | `test_x_*` | sandbox: reported as unsupported |
| Y | no secrets in reports/Slack | `test_y_*` | mocked |
| Z | backward compatibility | the full pre-existing suite | sandbox |
