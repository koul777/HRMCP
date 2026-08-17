import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


element_runner = load_script_module(
    "run_element_api_collection_safety",
    "scripts/run_element_api_collection.py",
)
element_watchdog = load_script_module(
    "watch_element_api_collection_safety",
    "scripts/watch_element_api_collection.py",
)


class ElementApiCollectionSafetyTests(unittest.TestCase):
    def test_batch_fully_rate_limited_requires_no_progress(self) -> None:
        result = {
            "summary": {
                "elements_requested": 100,
                "elements_rate_limited": 100,
            }
        }

        self.assertTrue(element_runner.batch_fully_rate_limited(result, 0, 0))
        self.assertFalse(element_runner.batch_fully_rate_limited(result, 1, 0))
        self.assertFalse(element_runner.batch_fully_rate_limited(result, 0, 1))

    def test_batch_fully_rate_limited_ignores_partial_rate_limit(self) -> None:
        result = {
            "summary": {
                "elements_requested": 100,
                "elements_rate_limited": 99,
            }
        }

        self.assertFalse(element_runner.batch_fully_rate_limited(result, 0, 0))

    def test_watchdog_continues_after_rate_limit_pause_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "watch.jsonl"
            argv = [
                "watch_element_api_collection.py",
                "--db-path",
                str(root / "unused.db"),
                "--pid-file",
                str(root / "missing.pid"),
                "--watchdog-pid-file",
                str(root / "watchdog.pid"),
                "--log-path",
                str(log_path),
                "--run-log-path",
                str(root / "run.jsonl"),
                "--summary-path",
                str(root / "summary.json"),
                "--max-sweeps",
                "2",
                "--sleep-between-sweeps",
                "0",
                "--poll-seconds",
                "1",
            ]
            counts = {
                "total": 10,
                "matched": 1,
                "not_collected": 9,
                "api_failed": 0,
                "no_data": 0,
            }
            complete_counts = {
                "total": 10,
                "matched": 10,
                "not_collected": 0,
                "api_failed": 0,
                "no_data": 0,
            }
            with (
                patch.object(sys, "argv", argv),
                patch.object(element_watchdog, "active_watchdog_processes", return_value=[]),
                patch.object(element_watchdog, "active_collection_processes", return_value=[]),
                patch.object(
                    element_watchdog,
                    "remaining_counts",
                    side_effect=[
                        counts,
                        counts,
                        counts,
                        counts,
                        complete_counts,
                    ],
                ),
                patch.object(
                    element_watchdog,
                    "run_full_sweep",
                    side_effect=[element_watchdog.RATE_LIMIT_PAUSE_EXIT_CODE, 0],
                ) as run_full_sweep,
                patch.object(element_watchdog, "sleep_with_cooldown_heartbeat") as cooldown_sleep,
                patch.object(element_watchdog.time, "sleep") as sleep,
            ):
                exit_code = element_watchdog.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(run_full_sweep.call_count, 2)
            cooldown_sleep.assert_called_once()
            self.assertEqual(cooldown_sleep.call_args.args[1], 1800.0)
            self.assertEqual(cooldown_sleep.call_args.args[2], 1)
            sleep.assert_not_called()
            events = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            event_names = [event.get("event") for event in events]
            self.assertIn("rate_limit_pause_from_sweep", event_names)
            self.assertNotIn("rate_limit_cooldown_heartbeat", event_names)

    def test_watchdog_honors_recovered_rate_limit_cooldown_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "watch.jsonl"
            log_path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-06-24T13:00:00+00:00",
                        "event": "rate_limit_pause_from_sweep",
                        "sweep": 3,
                        "returncode": element_watchdog.RATE_LIMIT_PAUSE_EXIT_CODE,
                        "cooldown_seconds": 120,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            argv = [
                "watch_element_api_collection.py",
                "--db-path",
                str(root / "unused.db"),
                "--pid-file",
                str(root / "missing.pid"),
                "--watchdog-pid-file",
                str(root / "watchdog.pid"),
                "--log-path",
                str(log_path),
                "--run-log-path",
                str(root / "run.jsonl"),
                "--summary-path",
                str(root / "summary.json"),
                "--max-sweeps",
                "2",
                "--sleep-between-sweeps",
                "0",
                "--poll-seconds",
                "1",
            ]
            pending_counts = {
                "total": 10,
                "matched": 1,
                "not_collected": 9,
                "api_failed": 0,
                "no_data": 0,
            }
            complete_counts = {
                "total": 10,
                "matched": 10,
                "not_collected": 0,
                "api_failed": 0,
                "no_data": 0,
            }
            with (
                patch.object(sys, "argv", argv),
                patch.object(element_watchdog, "active_watchdog_processes", return_value=[]),
                patch.object(element_watchdog, "active_collection_processes", return_value=[]),
                patch.object(
                    element_watchdog,
                    "pending_rate_limit_cooldown",
                    return_value={
                        "sweep": 3,
                        "returncode": element_watchdog.RATE_LIMIT_PAUSE_EXIT_CODE,
                        "latest_pause_at": "2026-06-24T13:00:00+00:00",
                        "cooldown_seconds": 120,
                        "cooldown_remaining_seconds": 120.0,
                        "cooldown_until": "2026-06-24T13:02:00+00:00",
                    },
                ) as pending_cooldown,
                patch.object(
                    element_watchdog,
                    "remaining_counts",
                    side_effect=[
                        pending_counts,
                        pending_counts,
                        pending_counts,
                        pending_counts,
                        complete_counts,
                    ],
                ),
                patch.object(
                    element_watchdog,
                    "run_full_sweep",
                    side_effect=[0, 0],
                ) as run_full_sweep,
                patch.object(element_watchdog, "sleep_with_cooldown_heartbeat") as cooldown_sleep,
                patch.object(element_watchdog.time, "sleep") as sleep,
            ):
                exit_code = element_watchdog.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(pending_cooldown.call_count, 1)
            self.assertEqual(run_full_sweep.call_count, 2)
            cooldown_sleep.assert_called_once()
            self.assertEqual(cooldown_sleep.call_args.args[1], 120.0)
            self.assertEqual(cooldown_sleep.call_args.args[2], 3)
            sleep.assert_any_call(0)
            events = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            event_names = [event.get("event") for event in events]
            self.assertIn("rate_limit_cooldown_recovered_from_log", event_names)
            self.assertNotIn("rate_limit_cooldown_heartbeat", event_names)

    def test_run_main_continues_after_full_rate_limited_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "pid.txt"
            pid_file.write_text("999", encoding="utf-8")
            log_path = root / "run.jsonl"
            argv = [
                "run_element_api_collection.py",
                "--db-path",
                str(root / "unused.db"),
                "--pid-file",
                str(pid_file),
                "--log-path",
                str(log_path),
                "--summary-path",
                str(root / "summary.json"),
                "--major-code",
                "14",
                "--max-hours",
                "1",
                "--max-batches",
                "1",
            ]
            before = {
                "total": 10,
                "matched": 1,
                "not_collected": 9,
                "api_failed": 0,
                "no_data": 0,
            }
            after = {
                "total": 10,
                "matched": 1,
                "not_collected": 9,
                "api_failed": 0,
                "no_data": 0,
            }
            result = {
                "command": "python src/ncs_mcp/collect_api.py --mode elements ...",
                "returncode": 0,
                "elapsed_seconds": 1.0,
                "stdout_tail": "",
                "stderr_tail": "",
                "summary": {
                    "elements_requested": 100,
                    "elements_rate_limited": 100,
                },
            }
            with (
                patch.object(sys, "argv", argv),
                patch.object(element_runner, "claim_pid_file", return_value=[]),
                patch.object(element_runner, "start_blockers", return_value=[]),
                patch.object(element_runner, "all_statuses", side_effect=[[before], [after]]),
                patch.object(element_runner, "run_batch", return_value=result),
                patch.object(element_runner, "write_summary"),
                patch.object(element_runner, "write_alias_outputs"),
                patch.object(element_runner.time, "sleep"),
            ):
                exit_code = element_runner.main()

            self.assertEqual(exit_code, element_runner.RATE_LIMIT_PAUSE_EXIT_CODE)
            events = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            event_names = [event.get("event") for event in events]
            self.assertIn("rate_limit_observed", event_names)
            self.assertNotIn("rate_limit_pause", event_names)
            stop_event = next(event for event in events if event.get("event") == "stop")
            self.assertEqual(stop_event["stop_reason"], "rate_limit_pause_required")
            self.assertEqual(stop_event["returncode"], element_runner.RATE_LIMIT_PAUSE_EXIT_CODE)
            self.assertEqual(stop_event["cooldown_recommended_seconds"], 1800)
            self.assertEqual(stop_event["next_safe_action"], "wait_for_rate_limit_cooldown")
            self.assertEqual(stop_event["rate_limited_batch_count"], 1)
            self.assertEqual(stop_event["latest_rate_limited_batches"][0]["major_code"], "14")
            self.assertEqual(stop_event["latest_rate_limited_batches"][0]["elements_requested"], 100)
            self.assertEqual(stop_event["latest_rate_limited_batches"][0]["elements_rate_limited"], 100)

    def test_watchdog_pid_file_blocks_existing_watchdog_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "watchdog.pid"
            pid_file.write_text("123", encoding="utf-8")

            with (
                patch.object(
                    element_watchdog,
                    "process_command_line",
                    return_value="python scripts\\watch_element_api_collection.py",
                ),
                patch.object(element_watchdog, "active_watchdog_processes", return_value=[]),
            ):
                blockers = element_watchdog.claim_watchdog_pid_file(pid_file, 999)

            self.assertEqual(blockers, [{"pid": 123, "role": "watchdog_pid_file_owner"}])
            self.assertEqual(pid_file.read_text(encoding="utf-8"), "123")

    def test_watchdog_main_winner_survives_transient_concurrent_watchdog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "watch.jsonl"
            argv = [
                "watch_element_api_collection.py",
                "--db-path",
                str(root / "unused.db"),
                "--pid-file",
                str(root / "missing.pid"),
                "--watchdog-pid-file",
                str(root / "watchdog.pid"),
                "--log-path",
                str(log_path),
                "--run-log-path",
                str(root / "run.jsonl"),
                "--summary-path",
                str(root / "summary.json"),
                "--max-sweeps",
                "1",
                "--poll-seconds",
                "1",
            ]
            complete_counts = {
                "total": 10,
                "matched": 10,
                "not_collected": 0,
                "api_failed": 0,
                "no_data": 0,
            }

            with (
                patch.object(sys, "argv", argv),
                patch.object(
                    element_watchdog,
                    "active_watchdog_processes",
                    return_value=[{"pid": 1234, "role": "watchdog"}],
                ) as active_watchdogs,
                patch.object(element_watchdog, "active_collection_processes", return_value=[]),
                patch.object(element_watchdog, "remaining_counts", return_value=complete_counts),
            ):
                exit_code = element_watchdog.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(active_watchdogs.call_count, 0)
            events = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            event_names = [event.get("event") for event in events]
            self.assertIn("watch_start", event_names)
            self.assertIn("complete", event_names)
            self.assertNotIn("watch_start_blocked_active_watchdog_detected", event_names)

    def test_watchdog_main_loser_exits_when_pid_file_owner_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "watch.jsonl"
            watchdog_pid_file = root / "watchdog.pid"
            watchdog_pid_file.write_text("123", encoding="utf-8")
            argv = [
                "watch_element_api_collection.py",
                "--db-path",
                str(root / "unused.db"),
                "--pid-file",
                str(root / "missing.pid"),
                "--watchdog-pid-file",
                str(watchdog_pid_file),
                "--log-path",
                str(log_path),
                "--run-log-path",
                str(root / "run.jsonl"),
                "--summary-path",
                str(root / "summary.json"),
                "--max-sweeps",
                "1",
            ]

            with (
                patch.object(sys, "argv", argv),
                patch.object(
                    element_watchdog,
                    "process_command_line",
                    return_value="python scripts\\watch_element_api_collection.py",
                ),
                patch.object(element_watchdog, "active_watchdog_processes", return_value=[]),
                patch.object(sys, "stdout", io.StringIO()),
            ):
                exit_code = element_watchdog.main()

            self.assertEqual(exit_code, 2)
            self.assertEqual(watchdog_pid_file.read_text(encoding="utf-8"), "123")
            events = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(events[-1]["event"], "watch_start_blocked_active_watchdog_detected")
            self.assertEqual(events[-1]["action"], "exit_no_duplicate_watchdog")

    def test_pending_rate_limit_cooldown_uses_latest_active_pause(self) -> None:
        events = [
            {
                "event": "rate_limit_pause_from_sweep",
                "timestamp": "2026-06-20T00:00:00+00:00",
                "cooldown_seconds": 3600,
                "sweep": 1,
                "returncode": element_watchdog.RATE_LIMIT_PAUSE_EXIT_CODE,
            },
            {
                "event": "rate_limit_pause_from_sweep",
                "timestamp": "2026-06-20T01:00:00+00:00",
                "cooldown_seconds": 7200,
                "sweep": 2,
                "returncode": element_watchdog.RATE_LIMIT_PAUSE_EXIT_CODE,
            },
        ]

        cooldown = element_watchdog.pending_rate_limit_cooldown(
            events,
            now_epoch=element_watchdog.parse_timestamp("2026-06-20T01:30:00+00:00"),
        )

        self.assertIsNotNone(cooldown)
        assert cooldown is not None
        self.assertEqual(cooldown["sweep"], 2)
        self.assertEqual(cooldown["cooldown_remaining_seconds"], 5400.0)
        self.assertEqual(cooldown["cooldown_until"], "2026-06-20T03:00:00+00:00")

        elapsed = element_watchdog.pending_rate_limit_cooldown(
            events,
            now_epoch=element_watchdog.parse_timestamp("2026-06-20T03:00:01+00:00"),
        )
        self.assertIsNone(elapsed)

    def test_pending_rate_limit_cooldown_ignores_pause_after_later_resume_activity(self) -> None:
        events = [
            {
                "event": "rate_limit_pause_from_sweep",
                "timestamp": "2026-06-20T00:00:00+00:00",
                "cooldown_seconds": 7200,
                "sweep": 1,
                "returncode": element_watchdog.RATE_LIMIT_PAUSE_EXIT_CODE,
            },
            {
                "event": "full_sweep_start",
                "timestamp": "2026-06-20T00:30:00+00:00",
            },
        ]

        cooldown = element_watchdog.pending_rate_limit_cooldown(
            events,
            now_epoch=element_watchdog.parse_timestamp("2026-06-20T01:00:00+00:00"),
        )

        self.assertIsNone(cooldown)


if __name__ == "__main__":
    unittest.main()
