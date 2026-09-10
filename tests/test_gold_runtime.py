from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.gold_runtime import (
    ENV_CONNECT_TIMEOUT_SECONDS,
    ENV_DATABASE,
    ENV_EMBEDDING_DIMENSIONS,
    ENV_ENABLED,
    ENV_PASSWORD,
    ENV_QUERY_TIMEOUT_SECONDS,
    ENV_URI,
    ENV_USERNAME,
    GoldRuntimeSettings,
    create_gold_runtime,
    load_gold_runtime_settings,
)
from ncs_mcp.neo4j_gold import Neo4jGoldUnavailableError
from ncs_mcp.retrieval import SQLiteRetriever


def _valid_env() -> dict[str, str]:
    return {
        ENV_ENABLED: "true",
        ENV_URI: "neo4j+s://secret-user:secret-uri-password@example.invalid",
        ENV_USERNAME: "secret-user",
        ENV_PASSWORD: "secret-password",
        ENV_DATABASE: "gold",
        ENV_EMBEDDING_DIMENSIONS: "3",
        ENV_CONNECT_TIMEOUT_SECONDS: "2.5",
        ENV_QUERY_TIMEOUT_SECONDS: "0.75",
    }


class _FakeDriver:
    def __init__(self, *, connectivity_error: Exception | None = None) -> None:
        self.connectivity_error = connectivity_error
        self.verify_calls = 0
        self.close_calls = 0
        self.execute_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    def verify_connectivity(self) -> None:
        self.verify_calls += 1
        if self.connectivity_error is not None:
            raise self.connectivity_error

    def execute_query(self, query: object, *args: object, **kwargs: object):
        if "timeout_" in kwargs:
            raise TypeError("Driver.execute_query does not support timeout_")
        self.execute_calls.append((query, args, dict(kwargs)))
        return ([], None, [])

    def close(self) -> None:
        self.close_calls += 1


class _FakeGraphDatabase:
    def __init__(self, driver: _FakeDriver) -> None:
        self.instance = driver
        self.calls: list[tuple[object, dict[str, object]]] = []

    def driver(self, uri: object, **kwargs: object) -> _FakeDriver:
        self.calls.append((uri, dict(kwargs)))
        return self.instance


class _FakeQuery:
    def __init__(self, text: object, *, timeout: object) -> None:
        self.text = text
        self.timeout = timeout


class _FakeNeo4jModule:
    def __init__(self, graph_database: _FakeGraphDatabase) -> None:
        self.GraphDatabase = graph_database
        self.Query = _FakeQuery


class GoldRuntimeTests(unittest.TestCase):
    def test_disabled_by_default_and_never_imports_driver(self) -> None:
        imports: list[str] = []
        gateway = create_gold_runtime(
            environ={}, importer=lambda name: imports.append(name)
        )

        self.assertFalse(gateway.enabled)
        self.assertEqual(gateway.status["state"], "disabled")
        self.assertFalse(gateway.status["available"])
        self.assertEqual(imports, [])

    def test_enabled_missing_configuration_is_safe_noop_without_import(self) -> None:
        imports: list[str] = []
        gateway = create_gold_runtime(
            environ={ENV_ENABLED: "1"}, importer=lambda name: imports.append(name)
        )

        self.assertEqual(gateway.status["state"], "invalid_config")
        self.assertIn("password_missing", gateway.status["issues"])
        self.assertIsNone(gateway.client)
        self.assertEqual(imports, [])

    def test_readiness_and_repr_never_expose_connection_secrets(self) -> None:
        settings = load_gold_runtime_settings(_valid_env())
        rendered = repr(settings) + repr(settings.readiness())

        self.assertNotIn("secret-password", rendered)
        self.assertNotIn("secret-uri-password", rendered)
        self.assertNotIn("secret-user", rendered)
        self.assertTrue(settings.readiness()["password_present"])
        self.assertTrue(settings.readiness()["uri_present"])

    def test_invalid_dimensions_do_not_import_driver(self) -> None:
        environment = _valid_env()
        environment[ENV_EMBEDDING_DIMENSIONS] = "4097"
        imports: list[str] = []

        gateway = create_gold_runtime(
            environ=environment, importer=lambda name: imports.append(name)
        )

        self.assertEqual(gateway.status["state"], "invalid_config")
        self.assertFalse(gateway.status["embedding_dimensions_valid"])
        self.assertIn("embedding_dimensions_invalid", gateway.status["issues"])
        self.assertEqual(imports, [])

    def test_connectivity_failure_is_typed_redacted_and_closes_driver(self) -> None:
        driver = _FakeDriver(
            connectivity_error=RuntimeError("secret-password backend detail")
        )
        graph_database = _FakeGraphDatabase(driver)
        module = _FakeNeo4jModule(graph_database)

        with self.assertRaises(Neo4jGoldUnavailableError) as raised:
            create_gold_runtime(environ=_valid_env(), importer=lambda name: module)

        self.assertNotIn("secret", str(raised.exception).lower())
        self.assertEqual(driver.verify_calls, 1)
        self.assertEqual(driver.close_calls, 1)

    def test_constructs_read_client_with_read_routing_and_bounded_timeout(self) -> None:
        driver = _FakeDriver()
        graph_database = _FakeGraphDatabase(driver)
        module = _FakeNeo4jModule(graph_database)
        imports: list[str] = []

        def importer(name: str) -> object:
            imports.append(name)
            return module

        gateway = create_gold_runtime(environ=_valid_env(), importer=importer)
        result = gateway.client.internal_role_subgraph("role-1")  # type: ignore[union-attr]

        self.assertEqual(imports, ["neo4j"])
        self.assertEqual(driver.verify_calls, 1)
        self.assertEqual(graph_database.calls[0][1]["connection_timeout"], 2.5)
        self.assertEqual(
            graph_database.calls[0][1]["auth"],
            ("secret-user", "secret-password"),
        )
        self.assertEqual(result["rows"], [])
        executed_query = driver.execute_calls[0][0]
        self.assertIsInstance(executed_query, _FakeQuery)
        self.assertEqual(executed_query.timeout, 0.75)
        self.assertIn("MATCH", str(executed_query.text))
        query_kwargs = driver.execute_calls[0][2]
        self.assertEqual(query_kwargs["database_"], "gold")
        self.assertEqual(query_kwargs["routing_"], "r")
        self.assertNotIn("timeout_", query_kwargs)

    def test_context_manager_closes_owned_driver_once(self) -> None:
        driver = _FakeDriver()
        module = _FakeNeo4jModule(_FakeGraphDatabase(driver))

        with create_gold_runtime(
            environ=_valid_env(), importer=lambda name: module
        ) as gateway:
            self.assertTrue(gateway.available)

        self.assertFalse(gateway.available)
        self.assertEqual(gateway.status["state"], "closed")
        self.assertEqual(driver.close_calls, 1)
        gateway.close()
        self.assertEqual(driver.close_calls, 1)

    def test_disabled_gateway_builds_sqlite_only_hybrid(self) -> None:
        sqlite = SQLiteRetriever(lambda query, limit: ["unit-1"])
        gateway = create_gold_runtime(environ={})
        hybrid = gateway.hybrid_retriever(sqlite)

        result = hybrid.retrieve("query", limit=5)
        self.assertEqual(result.candidate_ids, ("unit-1",))
        self.assertEqual(result.audit["augmenter"]["state"], "missing")

    def test_manual_invalid_settings_are_rejected_before_import(self) -> None:
        imports: list[str] = []
        settings = GoldRuntimeSettings(
            enabled=True,
            uri="neo4j://example.invalid",
            username="user",
            password="password",
            database="gold",
            embedding_dimensions=True,  # type: ignore[arg-type]
        )

        gateway = create_gold_runtime(
            settings, importer=lambda name: imports.append(name)
        )
        self.assertEqual(gateway.status["state"], "invalid_config")
        self.assertEqual(imports, [])


if __name__ == "__main__":
    unittest.main()
