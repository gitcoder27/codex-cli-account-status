# Repository Guidelines

## Project Structure & Module Organization
`planner.py` is the entire application: CLI argument parsing, `codex-profiles` integration, pacing logic, snapshot persistence, and terminal/JSON rendering all live there. Tests are in `tests/test_planner.py` and cover parsing, tracking, timeline generation, snapshot round-trips, and output rendering.

This repository currently has no package layout or build system. Treat it as a small Python CLI script with a pytest suite.

## Build, Test, and Development Commands
There is no build step. Use these commands from the repository root:

- `python3 planner.py --plain`: run the dashboard without ANSI styling.
- `python3 planner.py --json`: emit the machine-readable payload instead of the terminal view.
- `python3 -m pytest -q`: run the full test suite. This is the safest default in this repo.
- `PYTHONPATH=. pytest -q`: alternative pytest invocation if you prefer the `pytest` entrypoint.

The plain `pytest -q` command may fail to import `planner` in some environments, so prefer `python3 -m pytest -q`.

## Coding Style & Naming Conventions
Follow existing Python style in `planner.py`:

- 4-space indentation, no tabs.
- Use type hints throughout, including `list[...]`, `tuple[...]`, and `| None`.
- Prefer frozen `@dataclass` models for structured state.
- Keep helpers small and single-purpose; use `snake_case` for functions and variables, `UPPER_CASE` for module constants, and `CamelCase` for dataclasses.
- Stay within the standard library unless a clear project need justifies a dependency.

## Testing Guidelines
Add or update tests in [`tests/test_planner.py`](/home/ubuntu/Development/codex-cli-account-status/tests/test_planner.py) with every behavior change. Name tests descriptively with the `test_<behavior>` pattern. Cover both happy paths and edge cases such as malformed status output, reset detection, and snapshot/state handling.

## Commit & Pull Request Guidelines
Git history is mixed: there is at least one conventional commit (`feat: ...`) but also vague messages like `test` and `tst`. Prefer short, imperative commits with a type prefix when useful, for example `fix: handle missing auth.json` or `test: cover reset detection`.

Pull requests should include:

- A brief description of the user-visible change.
- Notes on test coverage, including the exact command run.
- Sample terminal or JSON output when dashboard rendering changes.

## Security & Configuration Tips
`planner.py` reads local Codex state such as `~/.codex/auth.json`, `~/.codex/profiles/profiles.json`, and the default snapshot file under `~/.codex/`. Do not commit real account data, auth material, or generated state files.
