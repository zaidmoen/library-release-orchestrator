# Release lab setup and live demo

## 1. Local lab (no network, about 1 minute)

```bash
pip install -e ".[dev]"
release-orchestrator lab-init --dest ./.lab
cd .lab
release-orchestrator plan --ticket FIN-204 --out plan.json   # B without A
release-orchestrator plan --ticket FIN-211                   # blocked: needs FIN-198
```

Run all fifteen scenarios and regenerate the evidence file:

```bash
python examples/lab/run_lab.py            # writes examples/lab/RESULTS.md
pytest -q tests/lab                       # the same scenarios as assertions
```

## 2. GitHub sandbox (one-time, done by a human)

The orchestrator never creates repositories. A person with access to the `zaidmoen`
account does this:

1. Create three **empty, private** repositories (no README, no license):
   `harri-release-lab-library`, `harri-release-lab-payment`, `harri-release-lab-invoice`.
2. If Claude's GitHub app is installed for *selected repositories*, add these three to it,
   so draft PRs can be created and read through the GitHub MCP server.
3. Seed them with the generated lab (pushes `main` and the three lab tags; never forced):

   ```bash
   python examples/lab/publish_lab_to_github.py --dest ./.live-lab --yes
   ```

`./.live-lab/release-orchestrator.yaml` uses `pull_requests.provider: external` and
allowlists exactly these three remotes.

## 3. Live demo script (about 10 minutes)

Run from `./.live-lab`. Every write asks for `--approve`. With the Skill, run
`/library-release FIN-204` and Claude drives the same steps.

| # | Command | What it shows |
|---|---|---|
| 1 | `release-orchestrator plan --ticket FIN-211` | Exit 3: `MISSING_PREREQUISITE` on FIN-198, even though the cherry-pick is clean |
| 2 | `release-orchestrator plan --ticket FIN-204 --out plan.json` | B planned without A: `1.1.0`, only `payment-service` targeted |
| 3 | `release-orchestrator prepare --plan plan.json` | Exit 4: approval required |
| 4 | `release-orchestrator prepare --plan plan.json --approve` and `validate --plan plan.json` | Isolated `release/1.1.0`, tests pass |
| 5 | `release-orchestrator pr --plan plan.json --approve` and `push --plan plan.json --approve` | PR request written; branch pushed (allowlisted, not forced) |
| 6 | Claude: GitHub MCP `create_pull_request` (draft), then `record-pr` | Live draft PR on the library repo |
| 7 | `publish --approve`, `push --approve` | Immutable artifact (local registry) plus tag `v1.1.0` on GitHub |
| 8 | `update-consumers --approve`, `push --approve`, MCP PR, `record-pr` | Draft PR on `payment-service` only; `invoice-service` untouched |
| 9 | `promote --version 1.1.0 --env prep --approved-by alice --approve` and so on | Ordered promotion with a quorum |
| 10 | Re-run steps 4–8 | Everything reports `already_*`; no duplicate branch, tag or PR |

Nothing is merged or deployed. Clean up by deleting the sandbox repositories on GitHub.
