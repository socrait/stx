## What this does and why

## Checks

- [ ] `uv run ruff check`
- [ ] `uv run ruff format --check`
- [ ] `uv run python -m pytest -q`
- [ ] `uv run python -m pytest -m docker -q` (if this touches the box runtime, overlay, or compose generation)
- [ ] `uv run python -m pytest -m build -q` (if this touches packaging or `pyproject.toml`)

## Related docs

Updated, if this changes behavior documented in `docs/` or `README.md`: <!-- list files, or "none" -->
