from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp import server
from ncs_mcp.config import load_settings
from ncs_mcp.db import connect
from ncs_mcp.smoke_data import create_ready_smoke_db


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReadOnlyServingTests(unittest.TestCase):
    def test_public_and_operator_reads_preserve_snapshot_without_read_only_flag(self) -> None:
        cases = (
            (server.ncs_search, {"query": "HR planning", "scope": "unit"}),
            (server.ncs_search, {}),
            (server.ncs_unit_detail, {"unit_code": "0202020101_23v3"}),
            (server.ncs_training, {"limit": 3}),
            (server.ncs_analysis, {"mode": "ontology"}),
            (server.get_quality_issues, {}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            create_ready_smoke_db(db_path)
            before = (db_path.stat().st_size, db_path.stat().st_mtime_ns, _sha256(db_path))
            before_files = sorted(Path(tmp).iterdir())
            settings = type("SettingsWithoutReadOnlyFlag", (), {"db_path": db_path})()
            with (
                patch.object(server, "load_settings", return_value=settings),
                patch.object(server, "connect", wraps=connect) as connect_mock,
                patch.object(server, "initialize_database") as initialize_mock,
            ):
                for handler, params in cases:
                    with self.subTest(tool=handler.__name__):
                        result = handler(**params)
                        if handler in (server.ncs_analysis, server.get_quality_issues):
                            # The smoke snapshot has no ontology concepts or quality issues.
                            self.assertEqual(result["error"]["code"], "NOT_FOUND")
                        else:
                            self.assertTrue(result["ok"], result)
                self.assertGreaterEqual(connect_mock.call_count, len(cases))
                for call in connect_mock.call_args_list:
                    self.assertEqual(call.args, (db_path,))
                    self.assertEqual(call.kwargs, {"read_only": True})
                initialize_mock.assert_not_called()
            self.assertEqual(
                (db_path.stat().st_size, db_path.stat().st_mtime_ns, _sha256(db_path)), before
            )
            self.assertEqual(sorted(Path(tmp).iterdir()), before_files)

    def test_public_reads_fail_closed_on_missing_or_unprepared_database(self) -> None:
        cases = (
            (server.ncs_search, {"query": "HR planning"}),
            (server.ncs_search, {}),
            (server.ncs_unit_detail, {"unit_code": "0202020101_23v3"}),
            (server.ncs_training, {}),
            (server.ncs_analysis, {"mode": "ontology"}),
            (server.get_quality_issues, {}),
        )
        for prepared_file in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = Path(tmp) / ("unprepared.db" if prepared_file else "missing/ncs.db")
                if prepared_file:
                    conn = sqlite3.connect(db_path)
                    conn.execute("CREATE TABLE unrelated (id INTEGER)")
                    conn.close()
                before = {
                    path.name: (path.stat().st_size, path.stat().st_mtime_ns, _sha256(path))
                    for path in Path(tmp).iterdir()
                }
                settings = type("SettingsWithoutReadOnlyFlag", (), {"db_path": db_path})()
                with (
                    patch.object(server, "load_settings", return_value=settings),
                    patch.object(server, "initialize_database") as initialize_mock,
                ):
                    for handler, params in cases:
                        with self.subTest(tool=handler.__name__, prepared_file=prepared_file):
                            result = handler(**params)
                            self.assertFalse(result["ok"])
                            self.assertIsInstance(result["error"], dict)
                            self.assertNotIn(str(db_path), json.dumps(result))
                    initialize_mock.assert_not_called()
                after = {
                    path.name: (path.stat().st_size, path.stat().st_mtime_ns, _sha256(path))
                    for path in Path(tmp).iterdir()
                }
                self.assertEqual(after, before)

    def test_builder_only_operations_stop_before_database_or_api_access(self) -> None:
        handlers = [
            server.retry_qualification_errors,
            server.build_training_course_ontology_links,
            server.import_career_paths,
            server.review_learning_module_ncs_link,
            server.review_training_goal_concept_link,
            server.review_task_ksa_concept_relation,
            server.review_ontology_concept,
            server.get_sqf_ontology_summary,
            *(handler for name, handler in vars(server.LEGACY_OPERATION_HANDLERS).items()
              if name != "recommend_learning_modules_by_ncs"),
        ]
        with (
            patch.object(server, "load_settings", side_effect=AssertionError("settings accessed")),
            patch.object(server, "open_db", side_effect=AssertionError("DB accessed")),
            patch.object(server, "qualification_retry_error_units") as retry_mock,
        ):
            for handler in handlers:
                with self.subTest(tool=handler.__name__):
                    # Reject before parameter parsing, credentials, file I/O, or collector calls.
                    result = handler()
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error"]["code"], "builder_only_operation")
            retry_mock.assert_not_called()

    def test_connect_read_only_blocks_writes_and_preserves_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            create_ready_smoke_db(db_path)
            before = (db_path.stat().st_size, db_path.stat().st_mtime_ns, _sha256(db_path))

            conn = connect(db_path, read_only=True)
            try:
                self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
                self.assertGreater(conn.execute("SELECT COUNT(*) FROM competency_units").fetchone()[0], 0)
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("CREATE TABLE forbidden_write (id INTEGER)")
            finally:
                conn.close()

            after = (db_path.stat().st_size, db_path.stat().st_mtime_ns, _sha256(db_path))

        self.assertEqual(after, before)

    def test_server_read_only_mode_skips_schema_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            create_ready_smoke_db(db_path)
            settings = type(
                "SettingsStub",
                (),
                {"db_path": db_path, "read_only_mode": True},
            )()
            with (
                patch.object(server, "load_settings", return_value=settings),
                patch.object(server, "initialize_database") as initialize_mock,
            ):
                conn = server.db()
            try:
                self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            finally:
                conn.close()
            initialize_mock.assert_not_called()

    def test_load_settings_reads_explicit_read_only_flag(self) -> None:
        previous = os.environ.get("NCS_MCP_READ_ONLY")
        os.environ["NCS_MCP_READ_ONLY"] = "1"
        try:
            settings = load_settings()
        finally:
            if previous is None:
                os.environ.pop("NCS_MCP_READ_ONLY", None)
            else:
                os.environ["NCS_MCP_READ_ONLY"] = previous

        self.assertTrue(settings.read_only_mode)

    def test_recommendations_are_read_only_without_serving_flag(self) -> None:
        cases = (
            ("recommend_training_for_task", {"query": "HR planning"}),
            ("recommend_training_transition", {
                "current_query": "HR planning", "target_query": "HR planning",
            }),
            ("plan_ncs_education_path", {
                "current_query": "HR planning", "target_query": "HR planning",
            }),
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            create_ready_smoke_db(db_path)
            before = (db_path.stat().st_size, db_path.stat().st_mtime_ns, _sha256(db_path))
            before_files = sorted(Path(tmp).iterdir())
            with (
                patch.dict(os.environ, {
                    "NCS_DB_PATH": str(db_path),
                    "NCS_MCP_READ_ONLY": "",
                    "NCS_MCP_READ_ONLY_MODE": "",
                    "NCS_MCP_ENABLE_ADVANCED_TOOLS": "1",
                }),
                patch.object(server, "connect", wraps=connect) as connect_mock,
                patch.object(server, "initialize_database") as initialize_mock,
            ):
                self.assertFalse(server.load_settings().read_only_mode)
                for tool_name, params in cases:
                    for via_meta in (False, True):
                        with self.subTest(tool=tool_name, via_meta=via_meta):
                            kwargs = {**params, "save": True}
                            result = (
                                server.ncs_execute_tool(tool_name, kwargs)
                                if via_meta else getattr(server, tool_name)(**kwargs)
                            )
                            self.assertTrue(result["ok"], result)
                            connect_mock.assert_called_with(db_path, read_only=True)
                self.assertEqual(connect_mock.call_count, 6)
                initialize_mock.assert_not_called()
            after = (db_path.stat().st_size, db_path.stat().st_mtime_ns, _sha256(db_path))
            self.assertEqual(after, before)
            self.assertEqual(sorted(Path(tmp).iterdir()), before_files)

    def test_recommendation_connection_rejects_accidental_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            create_ready_smoke_db(db_path)
            with patch.dict(os.environ, {
                "NCS_DB_PATH": str(db_path),
                "NCS_MCP_READ_ONLY": "0",
                "NCS_MCP_READ_ONLY_MODE": "0",
            }):
                with server.open_recommendation_db() as conn:
                    self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
                    with self.assertRaises(sqlite3.OperationalError):
                        conn.execute("CREATE TABLE forbidden_write (id INTEGER)")

    def test_missing_recommendation_database_fails_without_creating_files(self) -> None:
        cases = (
            ("recommend_training_for_task", {"query": "HR planning"}),
            ("recommend_training_transition", {
                "current_query": "General affairs", "target_query": "HR planning",
            }),
            ("plan_ncs_education_path", {
                "current_query": "General affairs", "target_query": "HR planning",
            }),
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "unprepared" / "ncs.db"
            with (
                patch.dict(os.environ, {
                    "NCS_DB_PATH": str(db_path),
                    "NCS_MCP_READ_ONLY": "0",
                    "NCS_MCP_READ_ONLY_MODE": "0",
                    "NCS_MCP_ENABLE_ADVANCED_TOOLS": "1",
                }),
                patch.object(server, "initialize_database") as initialize_mock,
            ):
                for tool_name, params in cases:
                    for via_meta in (False, True):
                        with self.subTest(tool=tool_name, via_meta=via_meta):
                            result = (
                                server.ncs_execute_tool(tool_name, params)
                                if via_meta else getattr(server, tool_name)(**params)
                            )
                            self.assertFalse(result["ok"])
                            self.assertEqual(result["error"]["code"], "tool_execution_failed")
                            self.assertEqual(result["error"]["exception_type"], "OperationalError")
                            self.assertNotIn(str(db_path), json.dumps(result))
                initialize_mock.assert_not_called()
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_unprepared_recommendation_database_is_not_initialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE unrelated (id INTEGER)")
            conn.close()
            before = (db_path.stat().st_mtime_ns, _sha256(db_path))
            with (
                patch.dict(os.environ, {
                    "NCS_DB_PATH": str(db_path),
                    "NCS_MCP_READ_ONLY": "0",
                    "NCS_MCP_READ_ONLY_MODE": "0",
                }),
                patch.object(server, "initialize_database") as initialize_mock,
            ):
                result = server.recommend_training_for_task(query="HR planning")
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "tool_execution_failed")
                initialize_mock.assert_not_called()
            self.assertEqual((db_path.stat().st_mtime_ns, _sha256(db_path)), before)
            self.assertEqual(sorted(Path(tmp).iterdir()), [db_path])

    def test_load_settings_accepts_legacy_read_only_mode_alias(self) -> None:
        previous = os.environ.get("NCS_MCP_READ_ONLY_MODE")
        os.environ["NCS_MCP_READ_ONLY_MODE"] = "true"
        try:
            with patch.dict(os.environ, {"NCS_MCP_READ_ONLY": ""}, clear=False):
                settings = load_settings()
            self.assertTrue(settings.read_only_mode)
        finally:
            if previous is None:
                os.environ.pop("NCS_MCP_READ_ONLY_MODE", None)
            else:
                os.environ["NCS_MCP_READ_ONLY_MODE"] = previous

    def test_load_settings_bounds_recommendation_capacity(self) -> None:
        with patch.dict(
            os.environ,
            {
                "NCS_MCP_MAX_CONCURRENT_RECOMMENDATIONS": "4",
                "NCS_MCP_RECOMMENDATION_QUEUE_TIMEOUT_SECONDS": "12.5",
            },
            clear=False,
        ):
            settings = load_settings()

        self.assertEqual(settings.max_concurrent_recommendations, 4)
        self.assertEqual(settings.recommendation_queue_timeout_seconds, 12.5)

    def test_public_recommendation_facades_suppress_save_requests(self) -> None:
        class DummyDb:
            def __enter__(self):
                return object()

            def __exit__(self, exc_type, exc, traceback):
                return False

        with (
            patch.object(server, "open_recommendation_db", return_value=DummyDb()),
            patch.object(
                server,
                "training_recommend_for_task",
                return_value={"ok": False, "error": "expected"},
            ) as task_mock,
        ):
            server.recommend_training_for_task(query="HR planning", save=True)
        self.assertFalse(task_mock.call_args.kwargs["save"])

        with (
            patch.object(server, "open_recommendation_db", return_value=DummyDb()),
            patch.object(
                server,
                "training_recommend_transition",
                return_value={"ok": False, "error": "expected"},
            ) as transition_mock,
        ):
            server.recommend_training_transition(
                current_query="General affairs",
                target_query="HR planning",
                save=True,
            )
        self.assertFalse(transition_mock.call_args.kwargs["save"])

    def test_public_recommendation_schemas_default_to_no_save(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        env["NCS_MCP_ENABLE_ADVANCED_TOOLS"] = "1"
        script = (
            "import asyncio, json; from ncs_mcp import server; "
            "print(json.dumps({tool.name: {'schema': tool.inputSchema, "
            "'description': tool.description} "
            "for tool in asyncio.run(server.mcp.list_tools()) "
            "if tool.name in ('recommend_training_for_task', 'recommend_training_transition')}))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, env=env,
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        contracts = json.loads(completed.stdout)
        for tool_name in ("recommend_training_for_task", "recommend_training_transition"):
            with self.subTest(tool=tool_name):
                self.assertIs(contracts[tool_name]["schema"]["properties"]["save"]["default"], False)
                self.assertIn("MCP never saves results", contracts[tool_name]["description"])

    def test_public_education_plan_suppresses_save_request(self) -> None:
        class DummyDb:
            def __enter__(self):
                return object()

            def __exit__(self, exc_type, exc, traceback):
                return False

        with (
            patch.object(server, "open_recommendation_db", return_value=DummyDb()),
            patch.object(
                server,
                "training_recommend_transition",
                return_value={"ok": False, "error": "expected"},
            ) as recommend_mock,
        ):
            server.plan_ncs_education_path(
                current_query="General affairs",
                target_query="HR planning",
                save=True,
            )
        self.assertFalse(recommend_mock.call_args.kwargs["save"])

    def test_public_recommendation_facades_wrap_execution_exceptions(self) -> None:
        class DummyDb:
            def __enter__(self):
                return object()

            def __exit__(self, exc_type, exc, traceback):
                return False

        cases = (
            (
                "recommend_training_for_task",
                "training_recommend_for_task",
                {"query": "HR planning"},
            ),
            (
                "recommend_training_transition",
                "training_recommend_transition",
                {
                    "current_query": "General affairs",
                    "target_query": "HR planning",
                },
            ),
            (
                "plan_ncs_education_path",
                "training_recommend_transition",
                {
                    "current_query": "General affairs",
                    "target_query": "HR planning",
                },
            ),
        )

        for tool_name, handler_name, kwargs in cases:
            with self.subTest(tool_name=tool_name):
                with (
                    patch.object(server, "open_recommendation_db", return_value=DummyDb()),
                    patch.object(
                        server,
                        handler_name,
                        side_effect=RuntimeError("private execution detail"),
                    ),
                ):
                    result = getattr(server, tool_name)(**kwargs)

                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "tool_execution_failed")
                self.assertEqual(result["error"]["category"], "execution")
                self.assertFalse(result["error"]["retryable"])
                self.assertEqual(result["error"]["tool_name"], tool_name)
                self.assertEqual(result["error"]["exception_type"], "RuntimeError")
                self.assertNotIn("private execution detail", json.dumps(result))
                self.assertTrue(result["capacity"]["acquired"])

    def test_public_recommendation_facades_wrap_setup_exceptions(self) -> None:
        with patch.object(
            server,
            "aihr_plan_route_evidence",
            side_effect=RuntimeError("private route detail"),
        ):
            route_result = server.plan_ncs_education_path(
                current_query="General affairs",
                target_query="HR planning",
            )

        self.assertFalse(route_result["ok"])
        self.assertEqual(route_result["error"]["code"], "tool_execution_failed")
        self.assertEqual(route_result["error"]["exception_type"], "RuntimeError")
        self.assertNotIn("private route detail", json.dumps(route_result))

        with patch.object(
            server,
            "recommendation_capacity_slot",
            side_effect=RuntimeError("private capacity detail"),
        ):
            capacity_result = server.recommend_training_for_task(query="HR planning")

        self.assertFalse(capacity_result["ok"])
        self.assertEqual(capacity_result["error"]["code"], "tool_execution_failed")
        self.assertEqual(
            capacity_result["capacity"]["status"],
            "capacity_not_initialized",
        )
        self.assertNotIn("private capacity detail", json.dumps(capacity_result))

    def test_read_only_mode_hides_operator_tools_even_when_requested(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        env["NCS_MCP_READ_ONLY"] = "1"
        env["NCS_MCP_ENABLE_OPERATOR_TOOLS"] = "1"
        script = (
            "import json; from ncs_mcp import server; "
            "surface=server.current_mcp_tool_surface(); "
            "print(json.dumps(surface))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        surface = json.loads(completed.stdout)

        self.assertTrue(surface["operator_tools_requested"])
        self.assertTrue(surface["operator_tools_blocked_by_read_only"])
        self.assertFalse(surface["operator_tools_enabled"])
        self.assertEqual(surface["operator_tools"], [])

    def test_operator_opt_in_preserves_audit_tools_but_cannot_enable_writes(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        env["NCS_MCP_READ_ONLY"] = "0"
        env["NCS_MCP_READ_ONLY_MODE"] = "0"
        env["NCS_MCP_ENABLE_OPERATOR_TOOLS"] = "1"
        script = (
            "import json; from ncs_mcp import server; "
            "print(json.dumps({'surface': server.current_mcp_tool_surface(), "
            "'review': server.review_ontology_concept(concept_id=1, review_status='candidate')}))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, env=env,
            capture_output=True, text=True, check=True,
        )
        result = json.loads(completed.stdout)
        self.assertIn("get_quality_issues", result["surface"]["operator_tools"])
        self.assertIn("review_ontology_concept", result["surface"]["operator_tools"])
        self.assertEqual(result["review"]["error"]["code"], "builder_only_operation")

    def test_http_transport_requires_explicit_remote_bind_opt_in(self) -> None:
        original_host = server.mcp.settings.host
        original_transport = server.CURRENT_TRANSPORT
        try:
            with self.assertRaisesRegex(ValueError, "non-loopback"):
                server.configure_transport(
                    transport="streamable-http",
                    host="0.0.0.0",
                    port=8766,
                )
            self.assertEqual(server.CURRENT_TRANSPORT, original_transport)

            server.configure_transport(
                transport="streamable-http",
                host="0.0.0.0",
                port=8766,
                allow_remote_bind=True,
            )
            self.assertEqual(server.CURRENT_TRANSPORT, "streamable-http")
            self.assertEqual(server.mcp.settings.host, "0.0.0.0")
        finally:
            server.mcp.settings.host = original_host
            server.CURRENT_TRANSPORT = original_transport

        for host in ("127.0.0.1", "::1", "localhost"):
            with self.subTest(host=host):
                self.assertTrue(server.is_loopback_bind_host(host))
        self.assertFalse(server.is_loopback_bind_host("example.internal"))

    def test_structure_search_resolves_registered_unit_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            create_ready_smoke_db(db_path)
            with patch.dict(
                os.environ,
                {"NCS_DB_PATH": str(db_path), "NCS_MCP_READ_ONLY": "1"},
                clear=False,
            ):
                result = server.ncs_search(query="HR planning", scope="unit", limit=3)

        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["id"], "0202020101_23v3")

    def test_recommendation_capacity_returns_retryable_busy_error(self) -> None:
        class DummyDb:
            def __enter__(self):
                return object()

            def __exit__(self, exc_type, exc, traceback):
                return False

        settings = type(
            "CapacitySettings",
            (),
            {
                "max_concurrent_recommendations": 1,
                "recommendation_queue_timeout_seconds": 0.1,
            },
        )()
        started = threading.Event()
        release = threading.Event()
        first_result: dict[str, object] = {}

        def slow_recommendation(*args, **kwargs):
            started.set()
            release.wait(timeout=5)
            return {"ok": True, "recommended_courses": []}

        def run_first() -> None:
            first_result.update(
                server.recommend_training_for_task(query="HR planning")
            )

        with (
            patch.object(server, "load_settings", return_value=settings),
            patch.object(server, "open_recommendation_db", return_value=DummyDb()),
            patch.object(
                server,
                "training_recommend_for_task",
                side_effect=slow_recommendation,
            ),
        ):
            thread = threading.Thread(target=run_first)
            thread.start()
            self.assertTrue(started.wait(timeout=2))
            busy_result = server.recommend_training_for_task(query="HR planning")
            release.set()
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertTrue(first_result["ok"])
        self.assertTrue(first_result["capacity"]["acquired"])
        self.assertFalse(busy_result["ok"])
        self.assertEqual(busy_result["error"]["code"], "service_busy")
        self.assertTrue(busy_result["error"]["retryable"])
        self.assertFalse(busy_result["error"]["capacity"]["acquired"])
        self.assertFalse(busy_result["capacity"]["acquired"])


if __name__ == "__main__":
    unittest.main()
