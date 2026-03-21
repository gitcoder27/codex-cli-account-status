from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import planner


SAMPLE_STATUS = """
[PLUS] ayan.second.mail@gmail.com (ayan.second)
91% left (resets in 52s)
83% left (resets in 5d)

[PLUS] ayan.saha.mails@gmail.com (ayan.saha)
100% left (resets in 5h)
94% left (resets in 4d)

[PLUS] ayushika128@gmail.com (ayushika)
100% left (resets in 5h)
99% left (resets in 4d)
""".strip()


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 3, 22, 12, 0, tzinfo=timezone.utc)

    def build_profiles_files(self, temp_path: Path) -> tuple[Path, Path]:
        auth_path = temp_path / "auth.json"
        profiles_path = temp_path / "profiles.json"
        auth_path.write_text(
            json.dumps({"tokens": {"account_id": "account-1"}}),
            encoding="utf-8",
        )
        profiles_path.write_text(
            json.dumps(
                {
                    "profiles": {
                        "ayan-second": {
                            "account_id": "account-1",
                            "email": "ayan.second.mail@gmail.com",
                            "label": "ayan.second",
                        },
                        "ayan-saha": {
                            "account_id": "account-2",
                            "email": "ayan.saha.mails@gmail.com",
                            "label": "ayan.saha",
                        },
                        "ayushika": {
                            "account_id": "account-3",
                            "email": "ayushika128@gmail.com",
                            "label": "ayushika",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        return auth_path, profiles_path

    def test_parse_relative_duration_supports_mixed_units(self) -> None:
        delta = planner.parse_relative_duration("1d 2h 3m 4s")
        self.assertEqual(delta, timedelta(days=1, hours=2, minutes=3, seconds=4))

    def test_parse_status_output_sorts_shorter_and_weekly_limits(self) -> None:
        accounts = planner.parse_status_output(SAMPLE_STATUS, self.now)
        self.assertEqual(len(accounts), 3)
        self.assertEqual(accounts[0].label, "ayan.second")
        self.assertEqual(accounts[0].short_limit.percent_left, 91.0)
        self.assertEqual(accounts[0].weekly_limit.percent_left, 83.0)
        self.assertLess(accounts[0].short_limit.reset_at, accounts[0].weekly_limit.reset_at)

    def test_mark_current_account_uses_auth_account_id(self) -> None:
        accounts = planner.parse_status_output(SAMPLE_STATUS, self.now)
        with tempfile.TemporaryDirectory() as temp_dir:
            auth_path, profiles_path = self.build_profiles_files(Path(temp_dir))
            marked = planner.mark_current_account(
                accounts,
                auth_path=auth_path,
                profiles_path=profiles_path,
            )

        current = next(account for account in marked if account.is_current)
        self.assertEqual(current.label, "ayan.second")
        self.assertEqual(current.account_id, "account-1")

    def test_window_target_increases_when_time_shrinks(self) -> None:
        account_far_reset = planner.AccountStatus(
            plan="PLUS",
            email="one@example.com",
            label="one",
            short_limit=planner.ParsedLimit(50.0, timedelta(hours=5), self.now + timedelta(hours=5)),
            weekly_limit=planner.ParsedLimit(50.0, timedelta(days=5), self.now + timedelta(days=5)),
        )
        account_near_reset = planner.AccountStatus(
            plan="PLUS",
            email="two@example.com",
            label="two",
            short_limit=planner.ParsedLimit(50.0, timedelta(hours=5), self.now + timedelta(hours=5)),
            weekly_limit=planner.ParsedLimit(50.0, timedelta(days=1), self.now + timedelta(days=1)),
        )

        far_metric = planner.build_metrics([account_far_reset], self.now, 3)[0]
        near_metric = planner.build_metrics([account_near_reset], self.now, 3)[0]
        self.assertGreater(near_metric.base_window_target_pct, far_metric.base_window_target_pct)

    def test_tracking_expands_window_when_behind(self) -> None:
        accounts = planner.parse_status_output(SAMPLE_STATUS, self.now)
        previous_snapshot = {
            "captured_at": (self.now - timedelta(hours=4)).isoformat(),
            "accounts": {
                "ayan.second": {
                    "email": "ayan.second.mail@gmail.com",
                    "weekly_left_pct": 90.0,
                    "weekly_pct_per_hour": 2.5,
                    "weekly_reset_at": (self.now + timedelta(days=5)).isoformat(),
                },
                "ayan.saha": {
                    "email": "ayan.saha.mails@gmail.com",
                    "weekly_left_pct": 96.0,
                    "weekly_pct_per_hour": 1.0,
                    "weekly_reset_at": (self.now + timedelta(days=4)).isoformat(),
                },
                "ayushika": {
                    "email": "ayushika128@gmail.com",
                    "weekly_left_pct": 99.0,
                    "weekly_pct_per_hour": 1.2,
                    "weekly_reset_at": (self.now + timedelta(days=4)).isoformat(),
                },
            },
        }

        metrics = planner.build_metrics(accounts, self.now, 3.0, previous_snapshot=previous_snapshot)
        ayan_second = next(metric for metric in metrics if metric.label == "ayan.second")
        self.assertEqual(ayan_second.tracking_status, "behind")
        self.assertGreater(ayan_second.adjusted_window_target_pct, ayan_second.base_window_target_pct)

    def test_projection_includes_now_row_and_adjusted_first_step(self) -> None:
        accounts = planner.parse_status_output(SAMPLE_STATUS, self.now)
        previous_snapshot = {
            "captured_at": (self.now - timedelta(hours=4)).isoformat(),
            "accounts": {
                "ayan.second": {
                    "email": "ayan.second.mail@gmail.com",
                    "weekly_left_pct": 90.0,
                    "weekly_pct_per_hour": 2.5,
                    "weekly_reset_at": (self.now + timedelta(days=5)).isoformat(),
                },
                "ayan.saha": {
                    "email": "ayan.saha.mails@gmail.com",
                    "weekly_left_pct": 96.0,
                    "weekly_pct_per_hour": 1.0,
                    "weekly_reset_at": (self.now + timedelta(days=4)).isoformat(),
                },
                "ayushika": {
                    "email": "ayushika128@gmail.com",
                    "weekly_left_pct": 99.0,
                    "weekly_pct_per_hour": 1.2,
                    "weekly_reset_at": (self.now + timedelta(days=4)).isoformat(),
                },
            },
        }

        metrics = planner.build_metrics(accounts, self.now, 3.0, previous_snapshot=previous_snapshot)
        ayan_second = next(metric for metric in metrics if metric.label == "ayan.second")
        label, horizon_hours, rows = planner.build_projection(ayan_second, self.now, 3.0)

        self.assertEqual(label, "ayan.second")
        self.assertEqual(horizon_hours, 24.0)
        self.assertEqual(rows[0].checkpoint_label, "now")
        self.assertEqual(rows[0].target_weekly_left_pct, ayan_second.weekly_left_pct)
        self.assertTrue(rows[1].uses_adjusted_target)
        self.assertAlmostEqual(rows[1].planned_burn_pct, ayan_second.adjusted_window_target_pct)
        self.assertAlmostEqual(
            rows[1].target_weekly_left_pct,
            ayan_second.weekly_left_pct - ayan_second.adjusted_window_target_pct,
        )
        expected_second_step_burn = ayan_second.adjusted_window_target_pct + (ayan_second.weekly_pct_per_hour * 3.0)
        self.assertAlmostEqual(rows[2].planned_burn_pct, expected_second_step_burn)
        self.assertFalse(rows[2].uses_adjusted_target)

    def test_projection_stops_at_weekly_reset_when_reset_is_earlier_than_24_hours(self) -> None:
        metric = planner.AccountMetrics(
            plan="PLUS",
            email="soon@example.com",
            label="soon",
            is_current=True,
            account_id=None,
            short_left_pct=50.0,
            short_reset_at=self.now + timedelta(hours=4),
            short_hours_left=4.0,
            weekly_left_pct=12.0,
            weekly_reset_at=self.now + timedelta(hours=7),
            weekly_hours_left=7.0,
            weekly_pct_per_hour=12.0 / 7.0,
            base_window_target_pct=(12.0 / 7.0) * 3.0,
            adjusted_window_target_pct=(12.0 / 7.0) * 3.0,
            short_window_advisory_pct=37.5,
            short_pressure="ok",
            tracking_status="new",
            tracking_delta_pct=None,
            actual_used_pct=None,
            expected_used_pct=None,
            elapsed_hours_since_last=None,
        )

        label, horizon_hours, rows = planner.build_projection(metric, self.now, 3.0)

        self.assertEqual(label, "soon")
        self.assertAlmostEqual(horizon_hours, 7.0)
        self.assertEqual(rows[-1].checkpoint_label, "+7h")
        self.assertEqual(rows[-1].note, "weekly reset")
        self.assertAlmostEqual(rows[-1].elapsed_hours, 7.0)

    def test_projection_returns_no_rows_for_non_positive_window(self) -> None:
        accounts = planner.parse_status_output(SAMPLE_STATUS, self.now)
        metrics = planner.build_metrics(accounts, self.now, 3.0)
        ayushika = next(metric for metric in metrics if metric.label == "ayushika")

        label, horizon_hours, rows = planner.build_projection(ayushika, self.now, 0.0)

        self.assertEqual(label, "ayushika")
        self.assertEqual(horizon_hours, 0.0)
        self.assertEqual(rows, [])

    def test_projection_prorates_adjusted_target_for_partial_first_window(self) -> None:
        metric = planner.AccountMetrics(
            plan="PLUS",
            email="partial@example.com",
            label="partial",
            is_current=True,
            account_id=None,
            short_left_pct=50.0,
            short_reset_at=self.now + timedelta(hours=2),
            short_hours_left=2.0,
            weekly_left_pct=10.0,
            weekly_reset_at=self.now + timedelta(hours=2),
            weekly_hours_left=2.0,
            weekly_pct_per_hour=2.0,
            base_window_target_pct=4.0,
            adjusted_window_target_pct=6.0,
            short_window_advisory_pct=50.0,
            short_pressure="ok",
            tracking_status="behind",
            tracking_delta_pct=-2.0,
            actual_used_pct=0.0,
            expected_used_pct=2.0,
            elapsed_hours_since_last=1.0,
        )

        _, horizon_hours, rows = planner.build_projection(metric, self.now, 3.0)

        self.assertAlmostEqual(horizon_hours, 2.0)
        self.assertEqual(rows[-1].checkpoint_label, "+2h")
        self.assertAlmostEqual(rows[-1].planned_burn_pct, 4.0)
        self.assertAlmostEqual(rows[-1].target_weekly_left_pct, 6.0)

    def test_timeline_orders_by_reset_and_flags_overload(self) -> None:
        metrics = [
            planner.AccountMetrics(
                plan="PLUS",
                email="late@example.com",
                label="late",
                is_current=False,
                account_id=None,
                short_left_pct=80.0,
                short_reset_at=self.now + timedelta(hours=5),
                short_hours_left=5.0,
                weekly_left_pct=50.0,
                weekly_reset_at=self.now + timedelta(hours=6),
                weekly_hours_left=6.0,
                weekly_pct_per_hour=8.333333333333334,
                base_window_target_pct=25.0,
                adjusted_window_target_pct=25.0,
                short_window_advisory_pct=48.0,
                short_pressure="ok",
                tracking_status="new",
                tracking_delta_pct=None,
                actual_used_pct=None,
                expected_used_pct=None,
                elapsed_hours_since_last=None,
            ),
            planner.AccountMetrics(
                plan="PLUS",
                email="earlier@example.com",
                label="earlier",
                is_current=True,
                account_id=None,
                short_left_pct=40.0,
                short_reset_at=self.now + timedelta(hours=5),
                short_hours_left=5.0,
                weekly_left_pct=150.0,
                weekly_reset_at=self.now + timedelta(hours=4),
                weekly_hours_left=4.0,
                weekly_pct_per_hour=37.5,
                base_window_target_pct=112.5,
                adjusted_window_target_pct=112.5,
                short_window_advisory_pct=24.0,
                short_pressure="tight",
                tracking_status="new",
                tracking_delta_pct=None,
                actual_used_pct=None,
                expected_used_pct=None,
                elapsed_hours_since_last=None,
            ),
        ]

        timeline, warnings = planner.build_timeline(metrics, self.now)
        self.assertEqual([block.label for block in timeline], ["earlier", "late"])
        self.assertTrue(any(block.overloaded for block in timeline))
        self.assertTrue(warnings)

    def test_snapshot_round_trip(self) -> None:
        accounts = planner.parse_status_output(SAMPLE_STATUS, self.now)
        metrics = planner.build_metrics(accounts, self.now, 3.0)
        snapshot = planner.build_snapshot(self.now, 3.0, metrics)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            planner.persist_snapshot(state_path, snapshot)
            loaded = planner.load_previous_snapshot(state_path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["captured_at"], self.now.isoformat())
        self.assertIn("ayan.second", loaded["accounts"])

    def test_collect_dashboard_data_returns_metrics_timeline_and_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            auth_path, profiles_path = self.build_profiles_files(temp_path)
            metrics, timeline, warnings = planner.collect_dashboard_data(
                SAMPLE_STATUS,
                self.now,
                3.0,
                auth_path=auth_path,
                profiles_path=profiles_path,
            )

        self.assertEqual(len(metrics), 3)
        self.assertEqual(len(timeline), 3)
        self.assertFalse(warnings)
        self.assertEqual(next(metric for metric in metrics if metric.is_current).label, "ayan.second")

    def test_render_dashboard_and_json_payload(self) -> None:
        accounts = planner.parse_status_output(SAMPLE_STATUS, self.now)
        metrics = planner.build_metrics(accounts, self.now, 3.0)
        timeline, warnings = planner.build_timeline(metrics, self.now)

        dashboard = planner.render_dashboard(self.now, 3.0, metrics, timeline, warnings, use_color=False)
        self.assertIn("Action Now", dashboard)
        self.assertIn("Weekly Projection", dashboard)
        self.assertIn("Rotation Plan", dashboard)
        self.assertIn("Account Health", dashboard)

        payload = planner.build_json_payload(self.now, 3.0, metrics, timeline, warnings)
        self.assertIn("accounts", payload)
        self.assertIn("projection", payload)
        self.assertEqual(payload["projection"]["account_label"], "ayushika")
        self.assertTrue(payload["projection"]["rows"])
        self.assertIn("timeline", payload)
        self.assertIn("action", payload)
        self.assertEqual(payload["window_hours"], 3.0)


if __name__ == "__main__":
    unittest.main()
