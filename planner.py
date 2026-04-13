#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

STATUS_HEADER_RE = re.compile(r"^\[(?P<plan>[^\]]+)\]\s+(?P<email>[^\s]+)\s+\((?P<label>[^)]+)\)$")
STATUS_LIMIT_RE = re.compile(
    r"^(?P<percent>\d+(?:\.\d+)?)%\s+left\s+\(resets\s+in\s+(?P<duration>[^)]+)\)$"
)
RELATIVE_PART_RE = re.compile(r"(?P<value>\d+)\s*(?P<unit>[smhdw])")
EPSILON_HOURS = 1e-9
TRACKING_TOLERANCE_PCT = 0.75
DEFAULT_STATE_PATH = Path.home() / ".codex" / "account-rotation-planner-state.json"


@dataclass(frozen=True)
class ParsedLimit:
    percent_left: float
    reset_in: timedelta
    reset_at: datetime


@dataclass(frozen=True)
class AccountStatus:
    plan: str
    email: str
    label: str
    short_limit: ParsedLimit
    weekly_limit: ParsedLimit
    is_current: bool = False
    account_id: str | None = None


@dataclass(frozen=True)
class StatusParseResult:
    accounts: list[AccountStatus]
    warnings: list[str]


@dataclass(frozen=True)
class AccountMetrics:
    plan: str
    email: str
    label: str
    is_current: bool
    account_id: str | None
    short_left_pct: float
    short_reset_at: datetime
    short_hours_left: float
    weekly_left_pct: float
    weekly_reset_at: datetime
    weekly_hours_left: float
    weekly_pct_per_hour: float
    base_window_target_pct: float
    adjusted_window_target_pct: float
    short_window_advisory_pct: float
    short_pressure: str
    tracking_status: str
    tracking_delta_pct: float | None
    actual_used_pct: float | None
    expected_used_pct: float | None
    elapsed_hours_since_last: float | None


@dataclass(frozen=True)
class TimelineBlock:
    label: str
    email: str
    start_at: datetime
    end_at: datetime
    duration_hours: float
    weekly_reset_at: datetime
    overloaded: bool


@dataclass(frozen=True)
class ProjectionRow:
    checkpoint_label: str
    timestamp: datetime
    elapsed_hours: float
    target_weekly_left_pct: float
    planned_burn_pct: float
    uses_adjusted_target: bool
    note: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan and track rotation across Codex profiles using weekly remaining budget percentages."
    )
    parser.add_argument(
        "--window-hours",
        type=float,
        default=3.0,
        help="Pacing window size in hours (default: 3).",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="Disable ANSI color styling in terminal output.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of the terminal dashboard.",
    )
    parser.add_argument(
        "--state-file",
        default=str(DEFAULT_STATE_PATH),
        help=f"Snapshot state file used for ahead/behind tracking (default: {DEFAULT_STATE_PATH}).",
    )
    return parser.parse_args(argv)


def run_status_command() -> str:
    try:
        completed = subprocess.run(
            ["codex-profiles", "status", "--all", "--json"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("`codex-profiles` was not found in PATH.") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "unknown error"
        raise RuntimeError(f"`codex-profiles status --all --json` failed: {stderr}")

    return completed.stdout


def parse_relative_duration(text: str) -> timedelta:
    parts = RELATIVE_PART_RE.findall(text.strip())
    if not parts:
        raise ValueError(f"Unsupported duration: {text!r}")

    consumed = "".join(f"{value}{unit}" for value, unit in parts)
    normalized = re.sub(r"\s+", "", text.strip())
    if consumed != normalized:
        raise ValueError(f"Unsupported duration: {text!r}")

    seconds = 0
    for value_text, unit in parts:
        value = int(value_text)
        if unit == "s":
            seconds += value
        elif unit == "m":
            seconds += value * 60
        elif unit == "h":
            seconds += value * 3600
        elif unit == "d":
            seconds += value * 86400
        elif unit == "w":
            seconds += value * 7 * 86400
        else:
            raise ValueError(f"Unsupported duration unit: {unit!r}")

    return timedelta(seconds=seconds)


def parse_plain_status_output(output: str, now: datetime) -> list[AccountStatus]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", output.strip()) if block.strip()]
    accounts: list[AccountStatus] = []

    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3:
            raise ValueError(f"Unexpected status block: {block!r}")

        header_match = STATUS_HEADER_RE.match(lines[0])
        if not header_match:
            raise ValueError(f"Unexpected profile header: {lines[0]!r}")

        parsed_limits: list[ParsedLimit] = []
        for line in lines[1:]:
            limit_match = STATUS_LIMIT_RE.match(line)
            if not limit_match:
                raise ValueError(f"Unexpected limit line: {line!r}")
            percent_left = float(limit_match.group("percent"))
            reset_in = parse_relative_duration(limit_match.group("duration"))
            parsed_limits.append(
                ParsedLimit(
                    percent_left=percent_left,
                    reset_in=reset_in,
                    reset_at=now + reset_in,
                )
            )

        if len(parsed_limits) < 2:
            raise ValueError(f"Expected at least two limit lines for {lines[0]!r}")

        ordered_limits = sorted(parsed_limits, key=lambda item: item.reset_in)
        accounts.append(
            AccountStatus(
                plan=header_match.group("plan"),
                email=header_match.group("email"),
                label=header_match.group("label"),
                short_limit=ordered_limits[0],
                weekly_limit=ordered_limits[-1],
            )
        )

    return accounts


def parse_timestamp_limit(limit_data: Any, now: datetime) -> ParsedLimit | None:
    if not isinstance(limit_data, dict):
        return None

    percent_left = limit_data.get("left_percent")
    reset_at_value = limit_data.get("reset_at")
    if not isinstance(percent_left, (int, float)) or not isinstance(reset_at_value, (int, float)):
        return None

    reset_at = datetime.fromtimestamp(float(reset_at_value), tz=timezone.utc).astimezone(now.tzinfo)
    reset_in = max(reset_at - now, timedelta(0))
    return ParsedLimit(
        percent_left=float(percent_left),
        reset_in=reset_in,
        reset_at=reset_at,
    )


def bucket_priority(bucket: dict[str, Any]) -> tuple[int, str]:
    bucket_id = str(bucket.get("id", "")).strip().lower()
    bucket_label = str(bucket.get("label", "")).strip().lower()
    if bucket_id == "codex" or bucket_label == "codex":
        return (0, bucket_label or bucket_id)
    return (1, bucket_label or bucket_id)


def parse_json_status_output(output: str, now: datetime) -> StatusParseResult:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON status output: {exc.msg}") from exc

    profiles = payload.get("profiles")
    if not isinstance(profiles, list):
        raise ValueError("JSON status output is missing a profiles list.")

    accounts: list[AccountStatus] = []
    warnings: list[str] = []

    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError(f"Unexpected profile payload: {profile!r}")

        plan = profile.get("plan")
        email = profile.get("email")
        label = profile.get("label")
        if not isinstance(plan, str) or not isinstance(email, str) or not isinstance(label, str):
            raise ValueError(f"Unexpected profile identity payload: {profile!r}")

        usage = profile.get("usage")
        if not isinstance(usage, dict):
            warnings.append(f"Skipped {label}: missing usage details in codex-profiles output.")
            continue

        buckets = usage.get("buckets")
        if not isinstance(buckets, list) or not buckets:
            warnings.append(f"Skipped {label}: no usage buckets were returned.")
            continue

        valid_buckets: list[tuple[tuple[int, str], dict[str, Any], ParsedLimit, ParsedLimit]] = []
        missing_weekly_bucket_labels: list[str] = []
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue

            short_limit = parse_timestamp_limit(bucket.get("five_hour"), now)
            weekly_limit = parse_timestamp_limit(bucket.get("weekly"), now)
            if short_limit and weekly_limit:
                valid_buckets.append((bucket_priority(bucket), bucket, short_limit, weekly_limit))
                continue

            bucket_name = str(bucket.get("label") or bucket.get("id") or "unknown bucket")
            if short_limit and weekly_limit is None:
                missing_weekly_bucket_labels.append(bucket_name)

        if not valid_buckets:
            if missing_weekly_bucket_labels:
                bucket_text = ", ".join(missing_weekly_bucket_labels)
                warnings.append(
                    f"Skipped {label}: missing weekly limit data for {bucket_text}."
                )
            else:
                warnings.append(
                    f"Skipped {label}: no bucket exposed both 5-hour and weekly limit data."
                )
            continue

        _, _, short_limit, weekly_limit = min(valid_buckets, key=lambda item: item[0])
        accounts.append(
            AccountStatus(
                plan=plan.upper(),
                email=email,
                label=label,
                short_limit=short_limit,
                weekly_limit=weekly_limit,
                is_current=bool(profile.get("is_current")),
            )
        )

    return StatusParseResult(accounts=accounts, warnings=warnings)


def parse_status_data(output: str, now: datetime) -> StatusParseResult:
    stripped = output.lstrip()
    if stripped.startswith("{"):
        return parse_json_status_output(stripped, now)
    return StatusParseResult(accounts=parse_plain_status_output(output, now), warnings=[])


def parse_status_output(output: str, now: datetime) -> list[AccountStatus]:
    return parse_status_data(output, now).accounts


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    if isinstance(data, dict):
        return data
    return {}


def mark_current_account(
    accounts: list[AccountStatus],
    auth_path: Path | None = None,
    profiles_path: Path | None = None,
) -> list[AccountStatus]:
    auth_path = auth_path or Path.home() / ".codex" / "auth.json"
    profiles_path = profiles_path or Path.home() / ".codex" / "profiles" / "profiles.json"

    auth_data = load_json_file(auth_path)
    profiles_data = load_json_file(profiles_path)

    current_account_id = None
    tokens = auth_data.get("tokens")
    if isinstance(tokens, dict):
        current_account_id = tokens.get("account_id")

    profiles = profiles_data.get("profiles", {})
    email_lookup: dict[str, dict[str, Any]] = {}
    label_lookup: dict[str, dict[str, Any]] = {}
    if isinstance(profiles, dict):
        for profile in profiles.values():
            if not isinstance(profile, dict):
                continue
            email = profile.get("email")
            label = profile.get("label")
            if isinstance(email, str):
                email_lookup[email] = profile
            if isinstance(label, str):
                label_lookup[label] = profile

    marked: list[AccountStatus] = []
    for account in accounts:
        profile = email_lookup.get(account.email) or label_lookup.get(account.label)
        account_id = profile.get("account_id") if profile else None
        is_current = account.is_current or bool(current_account_id and account_id == current_account_id)
        marked.append(
            AccountStatus(
                plan=account.plan,
                email=account.email,
                label=account.label,
                short_limit=account.short_limit,
                weekly_limit=account.weekly_limit,
                is_current=is_current,
                account_id=account_id if isinstance(account_id, str) else None,
            )
        )

    return marked


def hours_between(start: datetime, end: datetime) -> float:
    return max((end - start).total_seconds() / 3600.0, 0.0)


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def compute_short_pressure(window_target_pct: float, short_window_advisory_pct: float) -> str:
    if short_window_advisory_pct <= 0:
        return "blocked"
    ratio = window_target_pct / short_window_advisory_pct
    if ratio <= 0.8:
        return "ok"
    if ratio <= 1.0:
        return "watch"
    return "tight"


def load_previous_snapshot(state_path: Path) -> dict[str, Any] | None:
    state = load_json_file(state_path)
    snapshots = state.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        return None
    latest = snapshots[-1]
    return latest if isinstance(latest, dict) else None


def snapshot_accounts_map(previous_snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not previous_snapshot:
        return {}
    accounts = previous_snapshot.get("accounts")
    if isinstance(accounts, dict):
        return {key: value for key, value in accounts.items() if isinstance(value, dict)}
    return {}


def parse_snapshot_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def build_tracking_fields(
    label: str,
    metric_fields: dict[str, Any],
    previous_snapshot: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    previous_accounts = snapshot_accounts_map(previous_snapshot)
    previous = previous_accounts.get(label)
    if not previous:
        return {
            "tracking_status": "new",
            "tracking_delta_pct": None,
            "actual_used_pct": None,
            "expected_used_pct": None,
            "elapsed_hours_since_last": None,
            "adjusted_window_target_pct": metric_fields["base_window_target_pct"],
        }

    previous_captured_at = parse_snapshot_datetime(previous_snapshot.get("captured_at")) if previous_snapshot else None
    previous_weekly_reset_at = parse_snapshot_datetime(previous.get("weekly_reset_at"))
    if previous_captured_at is None or previous_weekly_reset_at is None:
        return {
            "tracking_status": "new",
            "tracking_delta_pct": None,
            "actual_used_pct": None,
            "expected_used_pct": None,
            "elapsed_hours_since_last": None,
            "adjusted_window_target_pct": metric_fields["base_window_target_pct"],
        }

    elapsed_hours = hours_between(previous_captured_at, now)
    if elapsed_hours <= EPSILON_HOURS:
        return {
            "tracking_status": "new",
            "tracking_delta_pct": None,
            "actual_used_pct": None,
            "expected_used_pct": None,
            "elapsed_hours_since_last": None,
            "adjusted_window_target_pct": metric_fields["base_window_target_pct"],
        }

    previous_weekly_left_pct = float(previous.get("weekly_left_pct", 0.0))
    previous_weekly_pct_per_hour = float(
        previous.get("weekly_pct_per_hour", metric_fields["weekly_pct_per_hour"])
    )
    current_weekly_left_pct = metric_fields["weekly_left_pct"]
    current_weekly_reset_at = metric_fields["weekly_reset_at"]

    reset_detected = (
        current_weekly_left_pct > previous_weekly_left_pct + 1.5
        or current_weekly_reset_at > previous_weekly_reset_at + timedelta(hours=6)
    )
    if reset_detected:
        return {
            "tracking_status": "reset",
            "tracking_delta_pct": None,
            "actual_used_pct": None,
            "expected_used_pct": None,
            "elapsed_hours_since_last": elapsed_hours,
            "adjusted_window_target_pct": metric_fields["base_window_target_pct"],
        }

    expected_used_pct = clamp(
        previous_weekly_pct_per_hour * elapsed_hours,
        0.0,
        previous_weekly_left_pct,
    )
    actual_used_pct = max(previous_weekly_left_pct - current_weekly_left_pct, 0.0)
    delta_pct = actual_used_pct - expected_used_pct
    adjusted_window_target_pct = clamp(
        metric_fields["base_window_target_pct"] - delta_pct,
        0.0,
        current_weekly_left_pct,
    )

    tolerance = max(TRACKING_TOLERANCE_PCT, metric_fields["base_window_target_pct"] * 0.2)
    if abs(delta_pct) <= tolerance:
        tracking_status = "on_track"
    elif delta_pct > 0:
        tracking_status = "ahead"
    else:
        tracking_status = "behind"

    return {
        "tracking_status": tracking_status,
        "tracking_delta_pct": delta_pct,
        "actual_used_pct": actual_used_pct,
        "expected_used_pct": expected_used_pct,
        "elapsed_hours_since_last": elapsed_hours,
        "adjusted_window_target_pct": adjusted_window_target_pct,
    }


def build_metrics(
    accounts: list[AccountStatus],
    now: datetime,
    window_hours: float,
    previous_snapshot: dict[str, Any] | None = None,
) -> list[AccountMetrics]:
    metrics: list[AccountMetrics] = []
    effective_window = max(window_hours, 0.0)

    for account in accounts:
        weekly_hours_left = max(hours_between(now, account.weekly_limit.reset_at), EPSILON_HOURS)
        short_hours_left = max(hours_between(now, account.short_limit.reset_at), EPSILON_HOURS)
        weekly_pct_per_hour = account.weekly_limit.percent_left / weekly_hours_left
        short_pct_per_hour = account.short_limit.percent_left / short_hours_left
        base_window_target_pct = weekly_pct_per_hour * min(effective_window, weekly_hours_left)
        short_window_advisory_pct = short_pct_per_hour * min(effective_window, short_hours_left)

        metric_fields = {
            "plan": account.plan,
            "email": account.email,
            "label": account.label,
            "is_current": account.is_current,
            "account_id": account.account_id,
            "short_left_pct": account.short_limit.percent_left,
            "short_reset_at": account.short_limit.reset_at,
            "short_hours_left": short_hours_left,
            "weekly_left_pct": account.weekly_limit.percent_left,
            "weekly_reset_at": account.weekly_limit.reset_at,
            "weekly_hours_left": weekly_hours_left,
            "weekly_pct_per_hour": weekly_pct_per_hour,
            "base_window_target_pct": base_window_target_pct,
            "short_window_advisory_pct": short_window_advisory_pct,
            "short_pressure": compute_short_pressure(base_window_target_pct, short_window_advisory_pct),
        }
        tracking_fields = build_tracking_fields(account.label, metric_fields, previous_snapshot, now)
        metrics.append(AccountMetrics(**metric_fields, **tracking_fields))

    return metrics


def sort_key(metric: AccountMetrics) -> tuple[Any, ...]:
    return (
        metric.weekly_reset_at,
        -metric.weekly_pct_per_hour,
        0 if metric.is_current else 1,
        metric.label.lower(),
    )


def build_timeline(metrics: list[AccountMetrics], now: datetime) -> tuple[list[TimelineBlock], list[str]]:
    active_metrics = [metric for metric in metrics if metric.weekly_left_pct > 0]
    if not active_metrics:
        return [], []

    horizon_at = max(metric.weekly_reset_at for metric in active_metrics)
    horizon_hours = max(hours_between(now, horizon_at), EPSILON_HOURS)
    total_weekly_pct = sum(metric.weekly_left_pct for metric in active_metrics)
    if total_weekly_pct <= 0:
        return [], []

    global_pct_per_hour = total_weekly_pct / horizon_hours
    cursor = now
    warnings: list[str] = []
    timeline: list[TimelineBlock] = []

    for metric in sorted(active_metrics, key=sort_key):
        duration_hours = metric.weekly_left_pct / global_pct_per_hour if global_pct_per_hour > 0 else 0.0
        end_at = cursor + timedelta(hours=duration_hours)
        overloaded = end_at > metric.weekly_reset_at + timedelta(seconds=1)
        if overloaded:
            warnings.append(
                f"{metric.label} would miss its weekly reset at {format_timestamp(metric.weekly_reset_at)}."
            )
        timeline.append(
            TimelineBlock(
                label=metric.label,
                email=metric.email,
                start_at=cursor,
                end_at=end_at,
                duration_hours=duration_hours,
                weekly_reset_at=metric.weekly_reset_at,
                overloaded=overloaded,
            )
        )
        cursor = end_at

    return timeline, warnings


def recommend_account(metrics: list[AccountMetrics]) -> AccountMetrics | None:
    active_metrics = [metric for metric in metrics if metric.weekly_left_pct > 0]
    if not active_metrics:
        return None
    return sorted(active_metrics, key=sort_key)[0]


def build_projection(
    metric: AccountMetrics | None,
    now: datetime,
    window_hours: float,
    max_horizon_hours: float = 24.0,
) -> tuple[str | None, float, list[ProjectionRow]]:
    if metric is None or window_hours <= 0:
        return (metric.label if metric else None, 0.0, [])

    horizon_hours = min(max(metric.weekly_hours_left, 0.0), max(max_horizon_hours, 0.0))
    if horizon_hours <= EPSILON_HOURS:
        return (metric.label, 0.0, [])

    rows = [
        ProjectionRow(
            checkpoint_label="now",
            timestamp=now,
            elapsed_hours=0.0,
            target_weekly_left_pct=metric.weekly_left_pct,
            planned_burn_pct=0.0,
            uses_adjusted_target=False,
            note="current snapshot",
        )
    ]

    elapsed_hours = 0.0
    target_weekly_left_pct = metric.weekly_left_pct
    cumulative_burn_pct = 0.0
    checkpoint_index = 1

    while elapsed_hours + EPSILON_HOURS < horizon_hours:
        step_hours = min(window_hours, horizon_hours - elapsed_hours)
        if step_hours <= EPSILON_HOURS:
            break

        uses_adjusted_target = checkpoint_index == 1
        if uses_adjusted_target:
            scaled_adjusted_target_pct = metric.adjusted_window_target_pct * (step_hours / window_hours)
            step_burn_pct = clamp(scaled_adjusted_target_pct, 0.0, target_weekly_left_pct)
            if metric.tracking_status == "ahead":
                note = "adjusted lower"
            elif metric.tracking_status == "behind":
                note = "adjusted higher"
            elif metric.tracking_status == "on_track":
                note = "adjusted on-track"
            elif metric.tracking_status == "reset":
                note = "adjusted post-reset"
            else:
                note = "adjusted baseline"
        else:
            step_burn_pct = clamp(metric.weekly_pct_per_hour * step_hours, 0.0, target_weekly_left_pct)
            note = "base pace"

        elapsed_hours += step_hours
        cumulative_burn_pct += step_burn_pct
        target_weekly_left_pct = clamp(metric.weekly_left_pct - cumulative_burn_pct, 0.0, metric.weekly_left_pct)

        if abs(elapsed_hours - round(elapsed_hours)) <= EPSILON_HOURS:
            checkpoint_text = f"+{int(round(elapsed_hours))}h"
        else:
            checkpoint_text = f"+{elapsed_hours:.1f}h"

        if horizon_hours - elapsed_hours <= EPSILON_HOURS and horizon_hours < max_horizon_hours:
            note = "weekly reset"

        rows.append(
            ProjectionRow(
                checkpoint_label=checkpoint_text,
                timestamp=now + timedelta(hours=elapsed_hours),
                elapsed_hours=elapsed_hours,
                target_weekly_left_pct=target_weekly_left_pct,
                planned_burn_pct=cumulative_burn_pct,
                uses_adjusted_target=uses_adjusted_target,
                note=note,
            )
        )
        checkpoint_index += 1

    return (metric.label, horizon_hours, rows)


def build_snapshot(now: datetime, window_hours: float, metrics: list[AccountMetrics]) -> dict[str, Any]:
    return {
        "captured_at": now.isoformat(),
        "window_hours": window_hours,
        "accounts": {
            metric.label: {
                "email": metric.email,
                "weekly_left_pct": metric.weekly_left_pct,
                "weekly_pct_per_hour": metric.weekly_pct_per_hour,
                "weekly_reset_at": metric.weekly_reset_at.isoformat(),
            }
            for metric in metrics
        },
    }


def persist_snapshot(state_path: Path, snapshot: dict[str, Any], keep: int = 50) -> None:
    state = load_json_file(state_path)
    snapshots = state.get("snapshots")
    if not isinstance(snapshots, list):
        snapshots = []
    snapshots.append(snapshot)
    snapshots = snapshots[-keep:]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("w", encoding="utf-8") as handle:
        json.dump({"version": 1, "snapshots": snapshots}, handle, indent=2)


def format_timestamp(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def format_relative_time(start: datetime, end: datetime) -> str:
    seconds = max(int((end - start).total_seconds()), 0)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append("0m")
    return " ".join(parts[:2])


def format_hours(value: float) -> str:
    if value >= 24:
        return f"{value / 24:.1f}d"
    return f"{value:.1f}h"


def format_pct(value: float) -> str:
    return f"{value:.2f}%"


def pressure_label(pressure: str) -> str:
    labels = {
        "ok": "OK",
        "watch": "WATCH",
        "tight": "TIGHT",
        "blocked": "BLOCKED",
    }
    return labels.get(pressure, pressure.upper())


def tracking_summary(metric: AccountMetrics) -> str:
    delta = metric.tracking_delta_pct
    if metric.tracking_status == "new":
        return "no history"
    if metric.tracking_status == "reset":
        return "weekly reset detected"
    if delta is None:
        return "no history"
    if metric.tracking_status == "on_track":
        return f"on track ({format_pct(abs(delta))})"
    if metric.tracking_status == "ahead":
        return f"ahead {format_pct(delta)}"
    return f"behind {format_pct(abs(delta))}"


def short_window_summary(metric: AccountMetrics) -> str:
    active = (
        metric.short_pressure != "ok"
        or metric.short_window_advisory_pct <= metric.adjusted_window_target_pct * 1.25
    )
    if not active:
        return "free"
    if metric.short_pressure == "blocked":
        return "blocked"
    return f"{pressure_label(metric.short_pressure).lower()} {format_pct(metric.short_window_advisory_pct)}"


class Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    WHITE = "\033[37m"


def style(text: str, color: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{color}{text}{Ansi.RESET}"


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def visible_len(text: str) -> int:
    return len(strip_ansi(text))


def terminal_width(default: int = 110) -> int:
    return max(80, min(shutil.get_terminal_size((default, 24)).columns, 140))


def pad_visible(text: str, width: int) -> str:
    return text + (" " * max(width - visible_len(text), 0))


def color_for_tone(tone: str) -> str:
    return {
        "info": Ansi.CYAN,
        "primary": Ansi.BLUE,
        "good": Ansi.GREEN,
        "warn": Ansi.YELLOW,
        "bad": Ansi.RED,
        "muted": Ansi.DIM,
        "accent": Ansi.MAGENTA,
    }.get(tone, Ansi.WHITE)


def tone_text(text: str, tone: str, use_color: bool, bold: bool = False) -> str:
    if not use_color:
        return text
    prefix = ""
    if bold:
        prefix += Ansi.BOLD
    prefix += color_for_tone(tone)
    return f"{prefix}{text}{Ansi.RESET}"


def badge(text: str, tone: str, use_color: bool) -> str:
    value = f"[{text}]"
    return tone_text(value, tone, use_color, bold=True)


def meter(value: float, total: float = 100.0, width: int = 12, use_color: bool = False, tone: str = "info") -> str:
    if total <= 0:
        ratio = 0.0
    else:
        ratio = clamp(value / total, 0.0, 1.0)
    filled = int(round(ratio * width))
    filled = max(0, min(width, filled))
    bar = "#" * filled + "-" * (width - filled)
    bar_text = f"[{bar}]"
    return tone_text(bar_text, tone, use_color) if filled else bar_text


def kv_line(label: str, value: str, label_width: int = 13, tone: str = "muted", use_color: bool = False) -> str:
    rendered_label = tone_text(label.rjust(label_width), tone, use_color, bold=True)
    return f"{rendered_label}  {value}"


def panel(title: str, lines: list[str], use_color: bool, tone: str = "info", width: int | None = None) -> str:
    panel_width = width or terminal_width()
    inner_width = panel_width - 4
    top = f"+- {title} " + "-" * max(inner_width - len(title) - 1, 0) + "+"
    top = tone_text(top, tone, use_color, bold=True)
    body: list[str] = [top]
    for line in lines:
        wrapped = textwrap.wrap(
            line,
            width=inner_width,
            replace_whitespace=False,
            drop_whitespace=False,
        ) or [""]
        for part in wrapped:
            body.append(f"| {pad_visible(part, inner_width)} |")
    bottom = tone_text("+" + "-" * (panel_width - 2) + "+", tone, use_color)
    body.append(bottom)
    return "\n".join(body)


def colorize_tracking(summary: str, metric: AccountMetrics, use_color: bool) -> str:
    if not use_color:
        return summary
    color = {
        "ahead": Ansi.GREEN,
        "on_track": Ansi.CYAN,
        "behind": Ansi.YELLOW,
        "reset": Ansi.YELLOW,
        "new": Ansi.CYAN,
    }.get(metric.tracking_status, Ansi.CYAN)
    return style(summary, color, use_color)


def build_recommendation_reason(
    current_account: AccountMetrics | None,
    recommended: AccountMetrics | None,
) -> str:
    if recommended is None:
        return "All weekly budgets are exhausted."
    if current_account and current_account.label == recommended.label:
        return (
            f"Stay on {recommended.label} because it is still first in the rotation order "
            f"and resets at {format_timestamp(recommended.weekly_reset_at)}."
        )
    if current_account:
        if recommended.weekly_reset_at < current_account.weekly_reset_at - timedelta(minutes=1):
            return (
                f"Switch now because {recommended.label} resets earlier "
                f"({format_timestamp(recommended.weekly_reset_at)}) than {current_account.label} "
                f"({format_timestamp(current_account.weekly_reset_at)}), so later-reset budget should be saved."
            )
        if recommended.weekly_pct_per_hour > current_account.weekly_pct_per_hour + 0.05:
            return (
                f"Switch now because {recommended.label} needs a faster burn rate "
                f"({format_pct(recommended.weekly_pct_per_hour)}/h vs {format_pct(current_account.weekly_pct_per_hour)}/h)."
            )
    return f"Use {recommended.label} first because it leads the optimized rotation order."


def build_action_lines(
    current_account: AccountMetrics | None,
    recommended: AccountMetrics | None,
    timeline: list[TimelineBlock],
    now: datetime,
    use_color: bool,
    window_hours: float,
) -> list[str]:
    if recommended is None:
        lines = [badge("PAUSED", "warn", use_color) + " no usable weekly budget remains"]
        return lines

    switching = current_account is None or current_account.label != recommended.label
    action_badge = badge("SWITCH NOW", "accent", use_color) if switching else badge("STAY HERE", "good", use_color)
    weekly_bar = meter(recommended.weekly_left_pct, use_color=use_color, tone="good")
    lines = [
        f"{action_badge} {tone_text(recommended.label, 'primary', use_color, bold=True)}  "
        f"{tone_text('weekly left', 'muted', use_color)} {format_pct(recommended.weekly_left_pct)} {weekly_bar}"
    ]
    lines.append(kv_line("Why", build_recommendation_reason(current_account, recommended), use_color=use_color))
    lines.append(
        kv_line(
            f"Next {window_hours:g}h",
            f"{format_pct(recommended.adjusted_window_target_pct)} target  "
            f"{tone_text('(base ' + format_pct(recommended.base_window_target_pct) + ')', 'muted', use_color)}",
            use_color=use_color,
        )
    )

    if recommended.tracking_status == "behind" and recommended.tracking_delta_pct is not None:
        lines.append(
            kv_line(
                "Tracking",
                f"{badge('CATCH UP', 'warn', use_color)} behind by "
                f"{format_pct(abs(recommended.tracking_delta_pct))} since last run, so this window is expanded.",
                use_color=use_color,
            )
        )
    elif recommended.tracking_status == "ahead" and recommended.tracking_delta_pct is not None:
        lines.append(
            kv_line(
                "Tracking",
                f"{badge('AHEAD', 'good', use_color)} ahead by "
                f"{format_pct(recommended.tracking_delta_pct)} since last run, so this window is reduced.",
                use_color=use_color,
            )
        )
    elif recommended.tracking_status == "on_track":
        lines.append(kv_line("Tracking", f"{badge('ON TRACK', 'info', use_color)} pacing is healthy since last run.", use_color=use_color))
    elif recommended.tracking_status == "reset":
        lines.append(kv_line("Tracking", f"{badge('RESET', 'warn', use_color)} weekly reset detected since last snapshot.", use_color=use_color))
    else:
        lines.append(
            kv_line(
                "Tracking",
                f"{badge('BASELINE', 'info', use_color)} no prior snapshot yet. Run again later to unlock ahead/behind tracking.",
                use_color=use_color,
            )
        )

    short_summary = short_window_summary(recommended)
    if short_summary == "free":
        lines.append(kv_line("Short Window", f"{badge('CLEAR', 'good', use_color)} not limiting right now.", use_color=use_color))
    elif short_summary == "blocked":
        lines.append(
            kv_line(
                "Short Window",
                f"{badge('BLOCKED', 'bad', use_color)} use carefully until the short reset passes.",
                use_color=use_color,
            )
        )
    else:
        lines.append(
            kv_line(
                "Short Window",
                f"{badge('WATCH', 'warn', use_color)} advisory {short_summary}.",
                use_color=use_color,
            )
        )

    if timeline:
        lines.append(
            kv_line(
                "Next Switch",
                f"{format_timestamp(timeline[0].end_at)}  "
                f"{tone_text('(in ' + format_relative_time(now, timeline[0].end_at) + ')', 'muted', use_color)}",
                use_color=use_color,
            )
        )
    else:
        lines.append(kv_line("Next Switch", "n/a", use_color=use_color))

    if switching:
        lines.append(
            kv_line(
                "Command",
                tone_text(f"codex-profiles load --label {recommended.label}", "accent", use_color, bold=True),
                use_color=use_color,
            )
        )
    else:
        lines.append(kv_line("Command", "no switch needed now", use_color=use_color))
    return lines


def render_rotation_plan(
    timeline: list[TimelineBlock],
    now: datetime,
    use_color: bool,
    width: int,
) -> list[str]:
    lines: list[str] = []
    if not timeline:
        lines.append("No timeline available because all weekly budgets are exhausted.")
        return lines

    narrow = width < 100
    for index, block in enumerate(timeline, start=1):
        tone = "bad" if block.overloaded else ("accent" if index == 1 else "info")
        action = "use now" if index == 1 else "switch"
        step_badge = badge(f"STEP {index}", tone, use_color)
        if index == 1:
            prefix = f"{step_badge} {tone_text(block.label, 'primary', use_color, bold=True)}  start now"
        else:
            prefix = (
                f"{step_badge} {action} to {tone_text(block.label, 'primary', use_color, bold=True)}  "
                f"{tone_text('(in ' + format_relative_time(now, block.start_at) + ')', 'muted', use_color)}"
            )
        if narrow:
            block_line = prefix
            detail_line = (
                f"  until {format_timestamp(block.end_at)}  "
                f"{tone_text('for ' + format_hours(block.duration_hours), 'muted', use_color)}"
            )
        else:
            block_line = (
                f"{prefix} -> until {format_timestamp(block.end_at)}  "
                f"{tone_text('for ' + format_hours(block.duration_hours), 'muted', use_color)}"
            )
            detail_line = None
        if block.overloaded:
            block_line = tone_text(block_line, "bad", use_color)
        lines.append(block_line)
        if detail_line:
            lines.append(detail_line)
    return lines


def render_accounts_table(metrics: list[AccountMetrics], window_hours: float, use_color: bool, width: int) -> list[str]:
    if width < 110:
        return render_accounts_compact(metrics, window_hours, use_color)
    return render_accounts_wide(metrics, window_hours, use_color)


def render_accounts_compact(metrics: list[AccountMetrics], window_hours: float, use_color: bool) -> list[str]:
    recommended = recommend_account(metrics)
    sorted_metrics = sorted(metrics, key=sort_key)
    lines: list[str] = []
    for index, metric in enumerate(sorted_metrics):
        role_parts: list[str] = []
        if recommended and metric.label == recommended.label:
            role_parts.append(badge("NOW", "accent", use_color))
        if metric.is_current:
            role_parts.append(badge("CURRENT", "info", use_color))
        if not role_parts:
            role_parts.append(tone_text("LATER", "muted", use_color, bold=True))

        left_tone = "good"
        if metric.weekly_left_pct < 25:
            left_tone = "bad"
        elif metric.weekly_left_pct < 50:
            left_tone = "warn"

        tracking = colorize_tracking(tracking_summary(metric), metric, use_color)
        short = short_window_summary(metric)
        if use_color:
            if short not in {"free", "blocked"} and metric.short_pressure in {"watch", "tight"}:
                short = tone_text(short, "warn" if metric.short_pressure == "watch" else "bad", use_color)
            elif short == "blocked":
                short = tone_text(short, "bad", use_color)
            elif short == "free":
                short = tone_text(short, "good", use_color)

        lines.append(
            f"{' '.join(role_parts)} {tone_text(metric.label, 'primary', use_color, bold=True)}  "
            f"{format_pct(metric.weekly_left_pct)} {meter(metric.weekly_left_pct, use_color=use_color, tone=left_tone)}"
        )
        lines.append(
            f"  reset {format_timestamp(metric.weekly_reset_at)}  |  "
            f"pace {format_pct(metric.weekly_pct_per_hour)}/h  |  "
            f"{window_hours:g}h budget {format_pct(metric.adjusted_window_target_pct)}"
        )
        lines.append(f"  track {tracking}  |  short {short}")
        if index < len(sorted_metrics) - 1:
            lines.append("")
    return lines


def render_accounts_wide(metrics: list[AccountMetrics], window_hours: float, use_color: bool) -> list[str]:
    headers = [
        "Account",
        "Role",
        "Weekly Left",
        "Reset",
        "Pace/h",
        f"{window_hours:g}h Budget",
        "Track",
        "Short",
    ]
    recommended = recommend_account(metrics)
    rows: list[list[str]] = []

    for metric in sorted(metrics, key=sort_key):
        role_parts: list[str] = []
        if recommended and metric.label == recommended.label:
            role_parts.append(badge("NOW", "accent", use_color))
        if metric.is_current:
            role_parts.append(badge("CURRENT", "info", use_color))
        if not role_parts:
            role_parts.append(tone_text("later", "muted", use_color))
        role = " ".join(role_parts)

        track = tracking_summary(metric)
        short = short_window_summary(metric)
        left_tone = "good"
        if metric.weekly_left_pct < 25:
            left_tone = "bad"
        elif metric.weekly_left_pct < 50:
            left_tone = "warn"
        weekly_left = f"{format_pct(metric.weekly_left_pct)} {meter(metric.weekly_left_pct, use_color=use_color, tone=left_tone)}"
        if use_color:
            track = colorize_tracking(track, metric, use_color)
            if short not in {"free", "blocked"} and metric.short_pressure in {"watch", "tight"}:
                short = tone_text(short, "warn" if metric.short_pressure == "watch" else "bad", use_color)
            elif short == "blocked":
                short = tone_text(short, "bad", use_color)
            elif short == "free":
                short = tone_text(short, "good", use_color)

        rows.append(
            [
                tone_text(metric.label, "primary" if recommended and metric.label == recommended.label else "info", use_color, bold=bool(recommended and metric.label == recommended.label)),
                role,
                weekly_left,
                format_timestamp(metric.weekly_reset_at),
                tone_text(f"{format_pct(metric.weekly_pct_per_hour)}/h", "accent", use_color),
                tone_text(format_pct(metric.adjusted_window_target_pct), "good", use_color, bold=True),
                track,
                short,
            ]
        )

    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(strip_ansi(value)))

    rendered = []
    rendered.append(" ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    rendered.append(" ".join("-" * width for width in widths))
    for row in rows:
        padded: list[str] = []
        for index, value in enumerate(row):
            padding = widths[index] - visible_len(value)
            padded.append(value + (" " * padding))
        rendered.append(" ".join(padded))
    return rendered


def render_warnings(warnings: list[str], use_color: bool) -> list[str]:
    if not warnings:
        lines = [badge("CLEAR", "good", use_color) + " no warnings."]
        return lines
    lines = []
    lines.extend(f"{badge('WARN', 'warn', use_color)} {tone_text(warning, 'warn', use_color)}" for warning in warnings)
    return lines


def render_projection(
    rows: list[ProjectionRow],
    label: str | None,
    step_hours: float,
    horizon_hours: float,
    use_color: bool,
    width: int,
) -> list[str]:
    if not rows or label is None:
        return ["Projection unavailable for the current recommendation."]

    lines = [
        f"{tone_text(label, 'primary', use_color, bold=True)}  "
        f"{tone_text('every ' + format_hours(step_hours) + ' for ' + format_hours(horizon_hours), 'muted', use_color)}"
    ]
    if width < 110:
        for index, row in enumerate(rows):
            checkpoint = tone_text(
                row.checkpoint_label.upper(),
                "accent" if index else "info",
                use_color,
                bold=bool(index == 0),
            )
            lines.append(
                f"{checkpoint}  {format_timestamp(row.timestamp)}  "
                f"target {format_pct(row.target_weekly_left_pct)}  burn {format_pct(row.planned_burn_pct)}"
            )
            lines.append(f"  {row.note}")
            if index < len(rows) - 1:
                lines.append("")
        return lines

    headers = ["Checkpoint", "Time", "Target Weekly Left", "Planned Burn", "Track Note"]
    rows_text: list[list[str]] = []
    for row in rows:
        note = row.note
        if row.uses_adjusted_target:
            note = f"{note} *"
        rows_text.append(
            [
                row.checkpoint_label,
                format_timestamp(row.timestamp),
                format_pct(row.target_weekly_left_pct),
                format_pct(row.planned_burn_pct),
                note,
            ]
        )

    widths = [len(header) for header in headers]
    for row in rows_text:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    lines.append(" ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    lines.append(" ".join("-" * width_value for width_value in widths))
    for row in rows_text:
        lines.append(" ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    lines.append("* first future checkpoint uses the tracking-adjusted window target")
    return lines


def build_json_payload(
    now: datetime,
    window_hours: float,
    metrics: list[AccountMetrics],
    timeline: list[TimelineBlock],
    warnings: list[str],
) -> dict[str, Any]:
    current_account = next((metric for metric in metrics if metric.is_current), None)
    recommended = recommend_account(metrics)
    switching = recommended is not None and (current_account is None or current_account.label != recommended.label)
    projection_label, projection_horizon_hours, projection_rows = build_projection(recommended, now, window_hours)

    return {
        "generated_at": now.isoformat(),
        "window_hours": window_hours,
        "current_account": current_account.label if current_account else None,
        "recommended_account": recommended.label if recommended else None,
        "action": {
            "type": "switch" if switching else "stay",
            "label": recommended.label if recommended else None,
            "reason": build_recommendation_reason(current_account, recommended),
            "next_window_budget_pct": recommended.adjusted_window_target_pct if recommended else None,
            "base_window_budget_pct": recommended.base_window_target_pct if recommended else None,
            "next_load_command": (
                f"codex-profiles load --label {recommended.label}" if switching and recommended else None
            ),
            "next_switch_at": timeline[0].end_at.isoformat() if timeline else None,
        },
        "projection": {
            "account_label": projection_label,
            "step_hours": window_hours,
            "horizon_hours": projection_horizon_hours,
            "rows": [
                {
                    **asdict(row),
                    "timestamp": row.timestamp.isoformat(),
                }
                for row in projection_rows
            ],
        },
        "accounts": [
            {
                **asdict(metric),
                "short_reset_at": metric.short_reset_at.isoformat(),
                "weekly_reset_at": metric.weekly_reset_at.isoformat(),
            }
            for metric in sorted(metrics, key=sort_key)
        ],
        "timeline": [
            {
                **asdict(block),
                "start_at": block.start_at.isoformat(),
                "end_at": block.end_at.isoformat(),
                "weekly_reset_at": block.weekly_reset_at.isoformat(),
            }
            for block in timeline
        ],
        "warnings": warnings,
    }


def render_dashboard(
    now: datetime,
    window_hours: float,
    metrics: list[AccountMetrics],
    timeline: list[TimelineBlock],
    warnings: list[str],
    use_color: bool,
) -> str:
    current_account = next((metric for metric in metrics if metric.is_current), None)
    recommended = recommend_account(metrics)
    width = terminal_width()
    projection_label, projection_horizon_hours, projection_rows = build_projection(recommended, now, window_hours)
    title = tone_text("Codex Account Rotation Planner", "primary", use_color, bold=True)
    subtitle = tone_text(
        f"Generated {format_timestamp(now)}  |  window {window_hours:g}h  |  current "
        f"{current_account.label if current_account else 'none'}  |  recommended "
        f"{recommended.label if recommended else 'none'}",
        "muted",
        use_color,
    )
    sections = [
        panel("Action Now", build_action_lines(current_account, recommended, timeline, now, use_color, window_hours), use_color, tone="accent", width=width),
        panel("Weekly Projection", render_projection(projection_rows, projection_label, window_hours, projection_horizon_hours, use_color, width), use_color, tone="good", width=width),
        panel("Rotation Plan", render_rotation_plan(timeline, now, use_color, width), use_color, tone="info", width=width),
        panel("Account Health", render_accounts_table(metrics, window_hours, use_color, width), use_color, tone="primary", width=width),
        panel("Warnings", render_warnings(warnings, use_color), use_color, tone="warn" if warnings else "good", width=width),
    ]
    return "\n".join([title, subtitle, ""] + sections)


def collect_dashboard_data(
    status_output: str,
    now: datetime,
    window_hours: float,
    previous_snapshot: dict[str, Any] | None = None,
    auth_path: Path | None = None,
    profiles_path: Path | None = None,
) -> tuple[list[AccountMetrics], list[TimelineBlock], list[str]]:
    parse_result = parse_status_data(status_output, now)
    if not parse_result.accounts:
        raise ValueError("No profiles exposed both 5-hour and weekly limit data.")

    marked_accounts = mark_current_account(parse_result.accounts, auth_path=auth_path, profiles_path=profiles_path)
    metrics = build_metrics(marked_accounts, now, window_hours, previous_snapshot=previous_snapshot)
    timeline, warnings = build_timeline(metrics, now)
    return metrics, timeline, parse_result.warnings + warnings


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = datetime.now().astimezone()
    state_path = Path(args.state_file).expanduser()

    try:
        previous_snapshot = load_previous_snapshot(state_path)
        status_output = run_status_command()
        metrics, timeline, warnings = collect_dashboard_data(
            status_output=status_output,
            now=now,
            window_hours=args.window_hours,
            previous_snapshot=previous_snapshot,
        )
        persist_snapshot(state_path, build_snapshot(now, args.window_hours, metrics))
    except Exception as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(build_json_payload(now, args.window_hours, metrics, timeline, warnings), indent=2))
        return 0

    use_color = not args.plain and sys.stdout.isatty()
    print(render_dashboard(now, args.window_hours, metrics, timeline, warnings, use_color))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
