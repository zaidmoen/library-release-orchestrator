# Contributing

## Setup

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Checks (CI runs the same)

```bash
ruff check src tests .claude && ruff format --check src tests .claude
mypy --strict src/release_orchestrator
pytest -q
```

Integration tests build real Git repositories in temporary directories (about 3 minutes).
To run only the fast unit tests, use `pytest -q tests/unit`.

> Windows note: if mypy fails with "The filename or extension is too long", the venv
> path is too deep for its compiled extensions. Run `uvx --with types-PyYAML --with pyyaml mypy --strict src/release_orchestrator`
> or move the checkout to a shorter path.

## Ground rules

* Keep correctness decisions in the Python engine. The Skill only orchestrates.
* Every Git call goes through `gitutil.repo.Git`, which refuses destructive operations.
  Don't bypass it in product code (the demo builder is the only exception, because it
  creates fixture repositories).
* New write steps need an `--approve` gate, idempotency (detect existing results) and a
  run-state transition defined in `state.TRANSITIONS`.
* Don't claim an integration works unless a test exercises it. Label anything else
  **[planned]** in the docs.
* Use conventional commits (`feat:`, `fix:`, `docs:`, `test:`).
