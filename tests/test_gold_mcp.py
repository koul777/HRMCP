from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.gold_mcp import (  # noqa: E402
    ENV_LOCAL_EMBEDDING_ENABLED,
    ENV_LOCAL_EMBEDDING_MODEL,
    GoldMCPFacade,
)
from ncs_mcp.gold_runtime import (  # noqa: E402
    ENV_DATABASE,
    ENV_EMBEDDING_DIMENSIONS,
    ENV_ENABLED,
    ENV_PASSWORD,
    ENV_URI,
    ENV_USERNAME,
)


def _enabled_environment() -> dict[str, str]:
    return {
        ENV_ENABLED: "true",
        ENV_URI: "neo4j+s://secret-user:secret-uri-password@example.invalid",
        ENV_USERNAME: "secret-user",
        ENV_PASSWORD: "secret-password",
        ENV_DATABASE: "gold",
        ENV_EMBEDDING_DIMENSIONS: "3",
    }


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def internal_role_subgraph(self, role_id: str, *, limit: int):
        self.calls.append(("subgraph", (role_id, limit)))
        return {"rows": [{"internal_job_role_id": role_id}], "audit": {"graph_depth": 4}}

    def internal_role_job_ksa_summary(self, role_id: str, *, limit: int):
        self.calls.append(("summary", (role_id, limit)))
        return {"rows": [{"internal_job_role_id": role_id}], "audit": {"graph_depth": 2}}

    def vector_graph_expansion(self, embedding, *, entity_kind: str, top_k: int, limit: int):
        self.calls.append(("vector", (list(embedding), entity_kind, top_k, limit)))
        return {
            "rows": [{"performance_criterion_id": "criterion-1", "score": 0.9}],
            "audit": {"graph_depth": 4, "top_k": top_k},
        }


class _FakeGateway:
    def __init__(self, client: _FakeClient | None) -> None:
        self.client = client
        self.available = client is not None
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        self.available = False
        self.client = None


class _Provider:
    provider_name = "test_local"
    model = "cached/test-model"
    dimensions = 3
    enabled = True

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[list[str]] = []

    def embed_texts(self, texts):
        self.calls.append(list(texts))
        if self.error is not None:
            raise self.error
        return [(0.1, 0.2, 0.3)]


class GoldMCPFacadeTests(unittest.TestCase):
    def test_status_is_disabled_by_default_and_never_creates_gateway(self) -> None:
        calls = 0

        def create_gateway(_settings):
            nonlocal calls
            calls += 1
            raise AssertionError("must not create runtime for status")

        facade = GoldMCPFacade(environ={}, gateway_factory=create_gateway)
        result = facade.status()

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(calls, 0)
        self.assertTrue(result["authority"]["sqlite_authoritative"])
        self.assertFalse(result["embedding"]["model_loaded"])

    def test_disabled_role_context_is_safe_fallback(self) -> None:
        facade = GoldMCPFacade(environ={})
        result = facade.internal_role_context("role-1")

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["error"], "gold_disabled")
        self.assertEqual(result["context"]["rows"], [])
        self.assertTrue(result["authority"]["sqlite_authoritative"])

    def test_summary_and_full_role_context_use_bounded_client_contracts(self) -> None:
        client = _FakeClient()
        gateway = _FakeGateway(client)
        facade = GoldMCPFacade(
            environ=_enabled_environment(), gateway_factory=lambda _settings: gateway
        )

        summary = facade.internal_role_context("role-1", summary=True, limit=3)
        full = facade.internal_role_context("role-1", summary=False, limit=3)

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["context"]["audit"]["graph_depth"], 2)
        self.assertEqual(full["context"]["audit"]["graph_depth"], 4)
        self.assertEqual(client.calls, [("summary", ("role-1", 3)), ("subgraph", ("role-1", 3))])
        self.assertEqual(summary["authority"]["gold_result_role"], "candidate_context")

    def test_semantic_context_uses_only_explicit_injected_provider(self) -> None:
        client = _FakeClient()
        provider = _Provider()
        facade = GoldMCPFacade(
            environ=_enabled_environment(),
            gateway_factory=lambda _settings: _FakeGateway(client),
            embedding_provider=provider,
        )

        result = facade.semantic_context(
            "인력운영계획 수립", entity_kind="performance_criterion", top_k=2, limit=4
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(provider.calls, [["인력운영계획 수립"]])
        self.assertEqual(client.calls[0], ("vector", ([0.1, 0.2, 0.3], "performance_criterion", 2, 4)))
        self.assertEqual(result["request"]["embedding_provider"], "test_local")

    def test_semantic_context_never_creates_embedding_provider_when_disabled(self) -> None:
        calls = 0

        def local_factory(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return _Provider()

        facade = GoldMCPFacade(environ={}, local_provider_factory=local_factory)
        result = facade.semantic_context("query")

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(calls, 0)

    def test_env_local_provider_is_lazy_and_local_only_by_default(self) -> None:
        client = _FakeClient()
        created: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def local_factory(*args, **kwargs):
            created.append((args, kwargs))
            return _Provider()

        environment = _enabled_environment()
        environment.update(
            {
                ENV_LOCAL_EMBEDDING_ENABLED: "true",
                ENV_LOCAL_EMBEDDING_MODEL: "cached/ko-model",
            }
        )
        facade = GoldMCPFacade(
            environ=environment,
            gateway_factory=lambda _settings: _FakeGateway(client),
            local_provider_factory=local_factory,
        )
        self.assertEqual(created, [])
        facade.status()
        self.assertEqual(created, [])
        result = facade.semantic_context("query")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(created[0][0], ("cached/ko-model",))
        self.assertTrue(created[0][1]["local_files_only"])
        self.assertEqual(created[0][1]["dimensions"], 3)
        self.assertEqual(created[0][1]["prompt_name"], "query")

    def test_unavailable_errors_do_not_leak_backend_or_password(self) -> None:
        facade = GoldMCPFacade(
            environ=_enabled_environment(),
            gateway_factory=lambda _settings: (_ for _ in ()).throw(
                RuntimeError("secret-password sensitive endpoint")
            ),
        )
        result = facade.internal_role_context("role-1")
        rendered = repr(result)

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error"], "gold_backend_unavailable")
        self.assertNotIn("secret-password", rendered)
        self.assertNotIn("sensitive endpoint", rendered)
        self.assertNotIn("secret-uri-password", repr(facade.status()))

    def test_invalid_input_fails_before_backend_creation(self) -> None:
        calls = 0

        def factory(_settings):
            nonlocal calls
            calls += 1
            return _FakeGateway(_FakeClient())

        facade = GoldMCPFacade(environ=_enabled_environment(), gateway_factory=factory)
        result = facade.semantic_context(" ", entity_kind="not_allowed")

        self.assertEqual(result["status"], "invalid_input")
        self.assertEqual(result["error"], "query_invalid")
        self.assertEqual(calls, 0)

    def test_embedding_exception_is_normalized_without_secret(self) -> None:
        provider = _Provider(error=RuntimeError("secret-password local model path"))
        facade = GoldMCPFacade(
            environ=_enabled_environment(),
            gateway_factory=lambda _settings: _FakeGateway(_FakeClient()),
            embedding_provider=provider,
        )
        result = facade.semantic_context("query")

        self.assertEqual(result["error"], "gold_backend_unavailable")
        self.assertNotIn("secret-password", repr(result))


if __name__ == "__main__":
    unittest.main()
