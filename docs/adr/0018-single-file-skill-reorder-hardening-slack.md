# ADR-0018: One-file Skill with a pinned engine, verifiable reorder sequences, operations and Slack visibility

Status: accepted (2026-11-26)

## Context

Engineers must receive **one file** (`SKILL.md`), open any library repository, type
`/library-release`, and get an explainable, safe plan for the team's top use case:
reordering cumulative release candidates. For example, rc.22 = A and rc.23 = A + B, and
B must ship first. The team wants every significant operation visible in
`#library-release-orchestrator`.

## Decisions

### 1. Distribution: a pinned engine, fetched and verified by the Skill

`distribution/library-release/SKILL.md` is the only distributed file. It pins **one full
commit SHA** of this repository. Its `lro-bootstrap` block:

1. Detects Python 3.11+ (`python3`, `python`, `py -3`) and git, and fails with
   `LRO_ERROR=...` otherwise.
2. If `~/.lro/engines/<sha>/engine.json` exists, it runs `lro_engine.py verify`, offline:
   * `HEAD == sha`;
   * the tree is clean;
   * the tree SHA is unchanged;
   * the origin is allowed.
3. Otherwise, after the engineer's one-time yes, it:
   1. creates a fresh temp directory under `~/.lro/tmp`;
   2. runs `git fetch --depth 1 origin <sha>`, checks out `FETCH_HEAD` detached, and
      checks `rev-parse HEAD == sha`;
   3. **only then** runs `scripts/lro_engine.py install` *from that verified checkout*.

The installer (standard library only) then:
1. Takes a per-revision lock (`O_EXCL`; stale locks are broken).
2. Re-verifies the checkout and moves it to `engines/<sha>/src`.
3. Creates `engines/<sha>/venv`, installing only `[project].dependencies`, or no pip at all
   when there are none.
4. Writes `bin/lro`, a bash wrapper with absolute paths and no `cd`.
5. Writes `engine.json` **last**: its presence means a verified install.

Consequences:
* **No moving branch.** A branch name is rejected (`BAD_SHA`).
* **No silent update.** A new revision means a new `SKILL.md`.
* **Nothing from the company repository is sent anywhere.** The fetch is a plain
  `git fetch` of a public commit.
* **Offline works** once the engine is cached.
* **Concurrent invocations** share one install (the second reports `reused`).
* **The engineer's working directory is preserved.**

`LRO_ALLOWED_ORIGINS` can add mirror origins, for tests and air-gapped mirrors. The SHA
stays the trust anchor.

The engine version-matches its own workflow docs:
`engines/<sha>/src/.claude/skills/library-release/` (SKILL.md, references, templates).

### 2. Reorder: every step explicit, every step re-verified

`release-mode options --mode reorder-releases` now states the following for **every**
step:
* `tickets` (included) and `excluded` (in the candidate, not in this step);
* `version` and `expected_stable`;
* `new_candidate` (the next free `-rc.N` for that step's version, above every existing tag
  and every tag allocated earlier in the same sequence) or `uses_candidate` (the existing
  candidate when its content and version are exactly this step's);
* `cut_from` (the previous step's stable tag);
* `candidate_sha`.

Later steps are no longer silently "the old candidate": a candidate cut before B shipped
can't be released as is after B, so a new candidate is allocated (for example
`v1.2.0-rc.1`, cut from `v1.1.0`).

`release-mode progress --preview P` re-discovers the repository and verifies the whole
sequence. A step is DONE when its expected stable tag exists, contains the step's
changes, and contains none of a later step's.

Any of these is `DRIFT` (exit 3), and the engineer must re-plan:
* an original candidate tag moved;
* an unexpected stable release;
* a released tag with foreign content.

Each next step is planned and approved again against the current state.

Divergent candidates (not on the source branch) are **unsupported**. They're reported with
`DIVERGENT_CANDIDATE` evidence per tag; the only valid option is `stop`.

### 3. Operations and audit (`operations.py`)

`op start` creates a correlation ID (`op-<UTC>-<8 hex>`). A second active operation for
the same library is `CONCURRENT_OPERATION` until the first ends or is cancelled (24 h TTL).

Events are facts. PLAN_READY, REORDER_BLOCKED, RC_CREATED, PR_CREATED, RELEASE_COMPLETED
and VALIDATION_FAILED must cite engine JSON, and the engine checks that the JSON supports
the event (`EVENT_NOT_SUPPORTED_BY_EVIDENCE`). Claude can't announce a release the engine
didn't publish. A terminal event closes the operation.

Identity comes only from an authenticated MCP payload (Slack profile or GitHub `get_me`).
Otherwise the actor is `unverified`. A Slack mention is produced only from a user ID that
is in the payload.

Everything stored or sent is redacted:
* GitHub, Slack and AWS tokens;
* JWTs and bearer tokens;
* private keys;
* webhook URLs;
* `password=`/`token:` pairs;
* credentials in URLs.

`op report` writes JSON or Markdown. It's documented as a local record, **not** a
tamper-proof compliance system.

### 4. Slack through MCP (`notify.py`): policy, threads, idempotent outbox

The engine never calls Slack.

**Channel.** `notify channel` / `notify policy --enable --approve` resolves the channel
from a `slack_search_channels` result. It accepts the real Markdown shape (captured
read-only), or JSON. It needs exactly one exact, non-archived name, and the channel's ID is
stored. It never creates or guesses a channel.

**Policy.** Stored at `~/.lro/notify-policy.json`. Enabling it is one explicit approval;
after that, routine notifications need no repeated confirmation.

**Outbox.** It lives in the operation file:
* `notify plan` returns `post` (exact `channel_id`, `thread_ts`, message, marker) and marks
  the delivery `pending`;
* `notify record` parses the MCP response's permalink or `ts`, to `delivered` (the first
  delivery becomes the thread parent), `not_sent` (a definite Slack error) or `uncertain`;
* a pending or uncertain delivery makes `plan` answer `reconcile`. The history is then
  searched for `lro:<op>:<event>`, and the delivery is marked delivered or not_sent. It is
  never re-posted blindly.

A Slack failure never touches release state. Slack content is never approval.

### 5. Consumers

Impact rows gain:
* `classification`: AFFECTED / NOT AFFECTED / UNRESOLVED / BLOCKED;
* `evidence_level`: `code` / `manifest-only` / `none`.

NOT AFFECTED requires code-level analysis, or a manifest that declares no dependency. A
service that wasn't analysed is UNRESOLVED.

## Not done

* The live company environment isn't verified:
  * no message was sent to `#library-release-orchestrator`;
  * no company repository was used.
* Slack `slack_send_message`'s success format was not captured live. The parser accepts a
  permalink or a JSON `ts`, and anything else is `uncertain`, so reconciliation is required.
* Divergent-candidate reordering is unsupported by design (reported, not attempted).
* Branch protection and repository rules are not read by the engine.
* Jira writes are not supported (read-only by design).
