# Live GitHub evidence

**Status: NOT YET VERIFIED.** No live GitHub run has happened yet.

What happened so far (2026-11-08):

* Creating `zaidmoen/harri-release-lab-library`, `-payment` and `-invoice` through the
  GitHub MCP server failed with `403 Resource not accessible by integration`: the
  integration isn't allowed to create repositories. Nothing was created.
* `GITHUB_TOKEN` isn't set, so the Python REST adapter stays **MOCKED**.

Once a human has created the three empty private repositories (docs/lab-setup.md §2), this
file will record, from the actual run:

* the pushed refs and their SHAs,
* the draft PR numbers and URLs (library and `payment-service`) and their state read back
  through the GitHub MCP server,
* the absence of any branch or PR on `invoice-service`,
* the idempotent re-run (`already_pushed`, recorded PR reused).
