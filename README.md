# Codex CLI Account Status

`codex-cli-account-status` is a small Python CLI that reads the output of `codex-profiles status --all --plain`, combines it with local Codex auth/profile state, and renders a rotation dashboard for multiple accounts.

It answers a simple question:

> Which Codex profile should I use next, how long can I stay on it, and am I ahead or behind the previous run?

## What It Shows

The planner computes:

- The recommended account to use now.
- A weekly rotation plan ordered by reset time and burn rate.
- A pacing window budget for each account.
- A short-window advisory based on the shorter reset cycle.
- Ahead/behind tracking from the previous snapshot.
- A machine-readable JSON view for automation.

## Requirements

- Python 3.10 or newer.
- The `codex-profiles` command available in `PATH`.
- Local Codex state files under `~/.codex/`:
  - `auth.json`
  - `profiles/profiles.json`

The script does not depend on third-party Python packages.

## Quick Start

Run the terminal dashboard:

```bash
python3 planner.py
```

Use the plain, non-colored view:

```bash
python3 planner.py --plain
```

Emit JSON for automation:

```bash
python3 planner.py --json
```

## CLI Flags

- `--window-hours <hours>`: pacing window size in hours. Default: `3.0`.
- `--plain`: disable ANSI color styling in terminal output.
- `--json`: emit JSON instead of the terminal dashboard.
- `--state-file <path>`: location of the snapshot state file used for tracking. Default: `~/.codex/account-rotation-planner-state.json`.

## How It Works

1. The script runs `codex-profiles status --all --plain`.
2. It parses each account block into a plan, email, label, and two reset-based limits.
3. It marks the current account by matching local Codex auth/profile metadata.
4. It calculates weekly burn rates, pacing windows, and a recommended rotation order.
5. It loads the latest saved snapshot, if present, to detect ahead/behind drift or a reset.
6. It saves a new snapshot after each successful run.

The snapshot is stored as JSON and keeps the most recent runs in a single file. By default, that file is:

```text
~/.codex/account-rotation-planner-state.json
```

## Terminal Output

The dashboard is split into sections:

- `Action Now`: the recommended immediate action and the next load command.
- `Weekly Projection`: a short horizon burn projection for the recommended account.
- `Rotation Plan`: the ordered sequence of accounts and switch points.
- `Account Health`: per-account status, pacing, tracking, and short-window pressure.
- `Warnings`: any accounts that are projected to miss their weekly reset.

If the terminal is narrow, the tables collapse into a compact layout automatically.

## JSON Output

`--json` returns a structured payload with these top-level keys:

- `generated_at`
- `window_hours`
- `current_account`
- `recommended_account`
- `action`
- `projection`
- `accounts`
- `timeline`
- `warnings`

This mode is useful for scripts, dashboards, or alerting.

## Example Workflow

```bash
# Show the current recommendation
python3 planner.py --plain

# Inspect the machine-readable payload
python3 planner.py --json | jq

# Use a custom state file if you do not want to write to ~/.codex/
python3 planner.py --state-file /tmp/codex-account-state.json
```

## Testing

Run the test suite from the repository root:

```bash
python3 -m pytest -q
```

The tests cover:

- Relative duration parsing.
- Status parsing and limit ordering.
- Current-account detection.
- Window and tracking calculations.
- Projection generation.
- Snapshot round-tripping behavior.
- Rendering helpers.

## Repository Layout

- `planner.py`: the complete CLI implementation.
- `tests/test_planner.py`: pytest/unittest coverage for parsing, tracking, projection, and rendering behavior.

## Notes

- The planner only reads local Codex state; it does not modify Codex profiles.
- If `codex-profiles` is missing or exits with an error, the CLI prints an error and exits non-zero.
- The snapshot file is safe to delete if you want to reset tracking history.
