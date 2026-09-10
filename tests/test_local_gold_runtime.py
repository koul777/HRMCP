from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.gold_runtime import (  # noqa: E402
    ENV_DATABASE,
    ENV_EMBEDDING_DIMENSIONS,
    ENV_ENABLED,
    ENV_PASSWORD,
    ENV_URI,
    ENV_USERNAME,
    GOLD_RUNTIME_ENV_ALLOWLIST,
)
from ncs_mcp.gold_mcp import (  # noqa: E402
    ENV_LOCAL_EMBEDDING_ALLOW_DOWNLOAD,
    ENV_LOCAL_EMBEDDING_DEVICE,
    ENV_LOCAL_EMBEDDING_ENABLED,
    ENV_LOCAL_EMBEDDING_MODEL,
    GoldMCPFacade,
)
from ncs_mcp.local_gold_runtime import (  # noqa: E402
    LOCAL_GOLD_CHILD_ENV_ALLOWLIST,
    LOCAL_GOLD_CONTAINER,
    LOCAL_GOLD_EMBEDDING_MODEL,
    LocalGoldOptions,
    LocalGoldRuntimeError,
    build_local_gold_child_environment,
    inspect_local_gold_container,
    run_local_gold_target,
)


SECRET = "super-secret-local-password"


def _inspect_payload(
    *, running: bool = True, health: str | None = "healthy", host_ip: str = "127.0.0.1"
) -> list[dict[str, object]]:
    state: dict[str, object] = {
        "Status": "running" if running else "exited",
        "Running": running,
    }
    if health is not None:
        state["Health"] = {"Status": health}
    return [
        {
            "Name": f"/{LOCAL_GOLD_CONTAINER}",
            "State": state,
            "Config": {
                "Env": [
                    "UNRELATED=value",
                    f"NEO4J_AUTH=neo4j/{SECRET}",
                    "NEO4J_initial_dbms_default__database=neo4j",
                ]
            },
            "NetworkSettings": {
                "Ports": {
                    "7687/tcp": [{"HostIp": host_ip, "HostPort": "7687"}],
                    "7474/tcp": [{"HostIp": host_ip, "HostPort": "7474"}],
                }
            },
        }
    ]


class _InspectRunner:
    def __init__(self, payload: object | None = None) -> None:
        self.payload = _inspect_payload() if payload is None else payload
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> object:
        self.calls.append((list(argv), dict(kwargs)))
        return SimpleNamespace(returncode=0, stdout=json.dumps(self.payload), stderr="")


class _ProbeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    def internal_role_subgraph(
        self, role_id: str, *, limit: int, max_hops: int = 4
    ):
        self.calls.append((role_id, limit, max_hops))
        return {"rows": []}


class _Gateway:
    def __init__(self) -> None:
        self.available = True
        self.client = _ProbeClient()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _GatewayFactory:
    def __init__(self) -> None:
        self.environ: dict[str, str] | None = None
        self.gateway = _Gateway()

    def __call__(self, *, environ: dict[str, str]) -> _Gateway:
        self.environ = dict(environ)
        return self.gateway


class LocalGoldRuntimeTests(unittest.TestCase):
    def test_inspect_uses_fixed_command_and_never_returns_multiple_containers(self) -> None:
        runner = _InspectRunner()
        result = inspect_local_gold_container(runner=runner)

        self.assertEqual(result["Name"], f"/{LOCAL_GOLD_CONTAINER}")
        self.assertEqual(
            runner.calls[0][0],
            [
                "docker",
                "inspect",
                "--type",
                "container",
                LOCAL_GOLD_CONTAINER,
            ],
        )
        with self.assertRaisesRegex(LocalGoldRuntimeError, "shape_invalid"):
            inspect_local_gold_container(runner=_InspectRunner([*runner.payload, *runner.payload]))  # type: ignore[arg-type]

    def test_container_name_is_allowlisted_before_docker_execution(self) -> None:
        runner = _InspectRunner()
        with self.assertRaisesRegex(LocalGoldRuntimeError, "container_not_allowed"):
            inspect_local_gold_container(
                LocalGoldOptions(container_name="attacker-controlled"), runner=runner
            )
        self.assertEqual(runner.calls, [])

    def test_child_environment_replaces_all_inherited_gold_values(self) -> None:
        container = _inspect_payload()[0]
        base = {
            "PATH": "safe-path",
            "PYTHONPATH": "C:\\attacker-controlled",
            "pythonstartup": "C:\\attacker-controlled\\startup.py",
            "PYTHONINSPECT": "1",
            ENV_PASSWORD: "stale-secret",
            "NCS_MCP_GOLD_NOT_ALLOWED": "must-disappear",
            ENV_LOCAL_EMBEDDING_MODEL: "attacker/model",
        }
        child = build_local_gold_child_environment(
            container, base_environment=base  # type: ignore[arg-type]
        )

        self.assertEqual(child["PATH"], "safe-path")
        self.assertEqual(child[ENV_PASSWORD], SECRET)
        self.assertEqual(child[ENV_USERNAME], "neo4j")
        self.assertEqual(child[ENV_DATABASE], "neo4j")
        self.assertEqual(child[ENV_URI], "neo4j://127.0.0.1:7687")
        self.assertEqual(child[ENV_ENABLED], "true")
        self.assertEqual(child[ENV_EMBEDDING_DIMENSIONS], "1024")
        self.assertEqual(child[ENV_LOCAL_EMBEDDING_ENABLED], "true")
        self.assertEqual(child[ENV_LOCAL_EMBEDDING_MODEL], LOCAL_GOLD_EMBEDDING_MODEL)
        self.assertEqual(child[ENV_LOCAL_EMBEDDING_ALLOW_DOWNLOAD], "false")
        self.assertEqual(child[ENV_LOCAL_EMBEDDING_DEVICE], "cpu")
        self.assertNotIn("NCS_MCP_GOLD_NOT_ALLOWED", child)
        self.assertFalse(any(name.upper().startswith("PYTHON") for name in child))
        self.assertEqual(
            {name for name in child if name.startswith("NCS_MCP_GOLD_")},
            set(LOCAL_GOLD_CHILD_ENV_ALLOWLIST),
        )

    def test_child_environment_configures_gold_mcp_semantic_capability(self) -> None:
        child = build_local_gold_child_environment(
            _inspect_payload()[0], base_environment={}
        )
        facade = GoldMCPFacade(environ=child)
        status = facade.status()

        self.assertTrue(status["embedding"]["configured"])
        self.assertEqual(status["embedding"]["provider_mode"], "local")
        self.assertTrue(status["embedding"]["local_files_only"])
        self.assertEqual(status["embedding"]["model"], LOCAL_GOLD_EMBEDDING_MODEL)

    def test_status_is_sanitized_and_does_not_probe_or_launch(self) -> None:
        inspect_runner = _InspectRunner()
        gateway_factory = _GatewayFactory()
        launches: list[object] = []

        report = run_local_gold_target(
            inspect_runner=inspect_runner,
            gateway_factory=gateway_factory,
            launch_runner=lambda *args, **kwargs: launches.append((args, kwargs)),
        )
        rendered = json.dumps(report, ensure_ascii=False)

        self.assertTrue(report["ok"])
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["launched"])
        self.assertFalse(report["read_only_probe"]["attempted"])
        self.assertTrue(report["semantic_embedding"]["configured"])
        self.assertTrue(report["semantic_embedding"]["local_files_only"])
        self.assertFalse(report["semantic_embedding"]["values_exposed"])
        self.assertIsNone(gateway_factory.environ)
        self.assertEqual(launches, [])
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn("neo4j/", rendered)
        self.assertFalse(report["production_secret_manager_replacement"])

    def test_probe_uses_allowlisted_env_and_fixed_bounded_read(self) -> None:
        gateway_factory = _GatewayFactory()
        report = run_local_gold_target(
            target="probe",
            inspect_runner=_InspectRunner(),
            gateway_factory=gateway_factory,
            base_environment={"NCS_MCP_GOLD_EVIL": "secret"},
        )

        self.assertTrue(report["read_only_probe"]["ok"])
        self.assertEqual(set(gateway_factory.environ or {}), set(GOLD_RUNTIME_ENV_ALLOWLIST))
        self.assertEqual(gateway_factory.gateway.client.calls[0][1:], (1, 4))
        self.assertTrue(gateway_factory.gateway.closed)
        self.assertNotIn(SECRET, json.dumps(report))

    def test_mcp_is_dry_run_by_default_and_command_is_fixed(self) -> None:
        gateway_factory = _GatewayFactory()
        launches: list[object] = []
        report = run_local_gold_target(
            target="mcp",
            transport="streamable-http",
            mcp_port=8123,
            inspect_runner=_InspectRunner(),
            gateway_factory=gateway_factory,
            launch_runner=lambda *args, **kwargs: launches.append((args, kwargs)),
        )

        self.assertTrue(report["dry_run"])
        self.assertEqual(launches, [])
        argv = report["launch_plan"]["argv"]
        self.assertEqual(
            argv[1:3],
            [
                "-I",
                str(ROOT / "src" / "ncs_mcp" / "server.py"),
            ],
        )
        self.assertEqual(
            argv[3:],
            [
                "--transport",
                "streamable-http",
                "--host",
                "127.0.0.1",
                "--port",
                "8123",
            ],
        )

    def test_explicit_builder_launch_receives_secret_env_without_output_copy(self) -> None:
        gateway_factory = _GatewayFactory()
        launch_calls: list[tuple[list[str], dict[str, object]]] = []

        def launch_runner(argv: list[str], **kwargs: object) -> object:
            launch_calls.append((list(argv), dict(kwargs)))
            return SimpleNamespace(returncode=0)

        report = run_local_gold_target(
            target="builder",
            launch=True,
            inspect_runner=_InspectRunner(),
            gateway_factory=gateway_factory,
            launch_runner=launch_runner,
            base_environment={
                "PATH": "safe",
                "PYTHONPATH": "C:\\attacker-controlled",
                "PYTHONSTARTUP": "C:\\attacker-controlled\\startup.py",
            },
        )

        self.assertTrue(report["launched"])
        self.assertFalse(report["dry_run"])
        self.assertEqual(launch_calls[0][0][1], "-I")
        self.assertEqual(Path(launch_calls[0][0][2]).name, "run_ncs_builder.py")
        self.assertTrue(Path(launch_calls[0][0][2]).is_absolute())
        child_env = launch_calls[0][1]["env"]
        self.assertEqual(child_env[ENV_PASSWORD], SECRET)  # type: ignore[index]
        self.assertEqual(child_env[ENV_LOCAL_EMBEDDING_ENABLED], "true")  # type: ignore[index]
        self.assertEqual(child_env[ENV_LOCAL_EMBEDDING_MODEL], LOCAL_GOLD_EMBEDDING_MODEL)  # type: ignore[index]
        self.assertEqual(child_env[ENV_LOCAL_EMBEDDING_ALLOW_DOWNLOAD], "false")  # type: ignore[index]
        self.assertFalse(
            any(str(name).upper().startswith("PYTHON") for name in child_env)  # type: ignore[union-attr]
        )
        self.assertNotIn(SECRET, json.dumps(report))

    def test_stopped_or_unhealthy_container_fails_closed(self) -> None:
        for payload, code in (
            (_inspect_payload(running=False), "container_not_running"),
            (_inspect_payload(health="unhealthy"), "container_health_unhealthy"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(LocalGoldRuntimeError, code):
                    run_local_gold_target(inspect_runner=_InspectRunner(payload))

    def test_invalid_auth_and_non_loopback_bindings_fail_closed(self) -> None:
        bad_auth = _inspect_payload()
        bad_auth[0]["Config"] = {"Env": ["NEO4J_AUTH=none"]}
        remote = _inspect_payload(host_ip="192.0.2.10")
        all_ipv4 = _inspect_payload(host_ip="0.0.0.0")
        all_ipv6 = _inspect_payload(host_ip="::")
        for payload, code in (
            (bad_auth, "neo4j_auth_missing"),
            (remote, "bolt_host_not_local"),
            (all_ipv4, "bolt_host_not_local"),
            (all_ipv6, "bolt_host_not_local"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(LocalGoldRuntimeError, code):
                    run_local_gold_target(inspect_runner=_InspectRunner(payload))

    def test_timeout_and_backend_details_are_redacted(self) -> None:
        def timeout_runner(*args: object, **kwargs: object) -> object:
            raise subprocess.TimeoutExpired("contains-secret", 5)

        with self.assertRaises(LocalGoldRuntimeError) as raised:
            inspect_local_gold_container(runner=timeout_runner)
        self.assertEqual(raised.exception.code, "docker_inspect_timeout")
        self.assertNotIn("contains-secret", str(raised.exception))

        def failed_probe(*, environ: dict[str, str]) -> object:
            raise RuntimeError(f"backend exposed {environ[ENV_PASSWORD]}")

        with self.assertRaises(LocalGoldRuntimeError) as raised:
            run_local_gold_target(
                target="probe",
                inspect_runner=_InspectRunner(),
                gateway_factory=failed_probe,
            )
        self.assertEqual(raised.exception.code, "gold_read_probe_failed")
        self.assertNotIn(SECRET, str(raised.exception))

    def test_target_launch_and_numeric_inputs_are_allowlisted(self) -> None:
        for kwargs, code in (
            ({"target": "shell"}, "target_not_allowed"),
            ({"target": "status", "launch": True}, "launch_target_not_allowed"),
            ({"target": "mcp", "mcp_port": 0}, "mcp_port_invalid"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(LocalGoldRuntimeError, code):
                    run_local_gold_target(
                        inspect_runner=_InspectRunner(),
                        gateway_factory=_GatewayFactory(),
                        **kwargs,  # type: ignore[arg-type]
                    )

        inspect_runner = _InspectRunner()
        with self.assertRaisesRegex(LocalGoldRuntimeError, "mcp_transport_not_allowed"):
            run_local_gold_target(
                target="mcp",
                transport="shell",
                inspect_runner=inspect_runner,
            )
        self.assertEqual(inspect_runner.calls, [])

        for options, code in (
            (
                LocalGoldOptions(local_embedding_model="attacker/model"),
                "local_embedding_model_not_allowed",
            ),
            (
                LocalGoldOptions(local_embedding_device="remote"),
                "local_embedding_device_not_allowed",
            ),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(LocalGoldRuntimeError, code):
                    run_local_gold_target(options=options, inspect_runner=_InspectRunner())


if __name__ == "__main__":
    unittest.main()
