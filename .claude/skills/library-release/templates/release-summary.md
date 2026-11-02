**Release plan `{plan_id}`: {library} {previous_version} → {proposed_version}** ({bump}, provisional)
Status: `{status}` · Verdict: `{verdict}` · Base: `{base_ref}`

| | Changes |
|---|---|
| Included | {included keys with titles} |
| Excluded | {excluded keys with reasons} |
| Already released | {already_released or "none"} |

**Findings:** one line per finding: `SEVERITY KIND: message`. Write "none" if there are none.
**Blockers:** one line per blocker. Remediation options follow if the plan is blocked.
**Consumers:** `name: action (reason)` for each. Only `update` consumers get PRs.
**Validation to run:** `scope/name` list.
**Next step:** what you're about to do and whether it needs approval.
