---
description: Run the full code gate — ruff lint, mypy (strict), and pytest.
---

Run the repository's code quality gate and report results. Run each step and surface
any failures:

```bash
uv sync --all-packages && uv run ruff check packages && uv run mypy && uv run pytest -q
```

- `uv run ruff check packages` — lint the workspace packages (never `ruff check .`; that would lint `planning/`).
- `uv run mypy` — strict type-check (config in root `pyproject.toml`).
- `uv run pytest -q` — run the test suite.

If any step fails, summarize the failures and stop; do not mark related work done while
the gate is red. Do not run anything against the `planning/` OKF bundle here — that has
its own conformance tooling.
