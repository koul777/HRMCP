from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path, PurePosixPath

from ncs_mcp.vercel_snapshot import (
    COMPACT_ARCHIVE_NAME,
    COMPACT_DATABASE_SCHEMA,
    COMPACT_MANIFEST_NAME,
    COMPACT_MANIFEST_SCHEMA,
    COMPACT_POSTING_CODEC,
    COMPACT_SNAPSHOT_NAME,
    MAX_BUNDLED_DB_BYTES,
    MAX_SNAPSHOT_BYTES,
    external_db_override_allowed,
    inspect_compact_archive,
    materialize_compact_snapshot,
    readiness_required_min_rows,
    readiness_required_tables,
    sqlite_snapshot_is_usable,
)
from scripts.package_vercel_compact_snapshot import package_compact_snapshot
from scripts.package_vercel_compact_snapshot import CANONICAL_DEPLOY_ROOT as PACKAGE_DEPLOY_ROOT
from scripts.verify_vercel_compact_package import (
    CANONICAL_DEPLOY_ROOT as VERIFY_DEPLOY_ROOT,
    measure_function_bundle,
    verify_package,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class VercelSnapshotTests(unittest.TestCase):
    def _write_function_file_map(
        self,
        deployment_root: Path,
        bundle: Path,
        *,
        archive_bytes: bytes = b"compact-archive",
        manifest_bytes: bytes = b"{}",
        extra_mappings: dict[str, tuple[str, bytes]] | None = None,
    ) -> dict[str, str]:
        bundle.mkdir(parents=True, exist_ok=True)
        api_root = deployment_root / "api"
        api_root.mkdir(parents=True, exist_ok=True)
        (api_root / COMPACT_ARCHIVE_NAME).write_bytes(archive_bytes)
        (api_root / COMPACT_MANIFEST_NAME).write_bytes(manifest_bytes)
        mappings = {
            f"api/{COMPACT_ARCHIVE_NAME}": f"api/{COMPACT_ARCHIVE_NAME}",
            f"api/{COMPACT_MANIFEST_NAME}": f"api/{COMPACT_MANIFEST_NAME}",
        }
        for logical_path, (target_path, contents) in (extra_mappings or {}).items():
            target = deployment_root / Path(*PurePosixPath(target_path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
            mappings[logical_path] = target_path
        (bundle / ".vc-config.json").write_text(
            json.dumps({"filePathMap": mappings}, sort_keys=True),
            encoding="utf-8",
        )
        (bundle / "vc__handler__python.py").write_text(
            "handler = object()\n", encoding="utf-8"
        )
        return mappings

    def _create_database(self, path: Path) -> dict[str, dict[str, int]]:
        physical_counts = {
            "competency_units": 1,
            "performance_criteria": 1,
            "ksa_items": 1,
            "ncs_training_courses": 1,
            "ontology_relation_outgoing": 1,
            "ontology_relation_incoming": 1,
            "criteria_concept_forward": 1,
            "criteria_concept_inverse": 1,
        }
        logical_counts = {
            "criteria_concept_links_enriched": 3,
            "ontology_concept_relations": 2,
        }
        # The public compact surface currently exposes these canonical objects
        # directly.  Keep this synthetic fixture on the same three-kind count
        # contract as the exporter (physical, logical, servable).
        servable_counts = {
            "competency_units": 1,
            "performance_criteria": 1,
            "ksa_items": 1,
            "ncs_training_courses": 1,
        }
        with closing(sqlite3.connect(path)) as conn:
            for table_name, count in physical_counts.items():
                conn.execute(f'CREATE TABLE "{table_name}" (value TEXT)')
                conn.executemany(
                    f'INSERT INTO "{table_name}" VALUES (?)',
                    ((f"ready-{index}",) for index in range(count)),
                )
            conn.execute(
                "ALTER TABLE ontology_relation_outgoing ADD COLUMN target_count INTEGER"
            )
            conn.execute(
                "ALTER TABLE ontology_relation_incoming ADD COLUMN source_count INTEGER"
            )
            conn.execute(
                "ALTER TABLE criteria_concept_forward ADD COLUMN concept_count INTEGER"
            )
            conn.execute(
                "ALTER TABLE criteria_concept_inverse ADD COLUMN criteria_count INTEGER"
            )
            conn.execute("UPDATE ontology_relation_outgoing SET target_count = 2")
            conn.execute("UPDATE ontology_relation_incoming SET source_count = 2")
            conn.execute("UPDATE criteria_concept_forward SET concept_count = 3")
            conn.execute("UPDATE criteria_concept_inverse SET criteria_count = 3")
            conn.execute(
                """
                CREATE TABLE serving_snapshot_manifest (
                    manifest_key TEXT PRIMARY KEY,
                    manifest_value TEXT NOT NULL
                ) WITHOUT ROWID
                """
            )
            conn.executemany(
                "INSERT INTO serving_snapshot_manifest VALUES (?, ?)",
                (
                    ("schema", COMPACT_DATABASE_SCHEMA),
                    ("codec", COMPACT_POSTING_CODEC),
                ),
            )
            conn.execute(
                """
                CREATE TABLE serving_snapshot_table_counts (
                    object_name TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    count_kind TEXT NOT NULL,
                    PRIMARY KEY (object_name, count_kind)
                ) WITHOUT ROWID
                """
            )
            conn.executemany(
                "INSERT INTO serving_snapshot_table_counts VALUES (?, ?, 'physical')",
                physical_counts.items(),
            )
            conn.executemany(
                "INSERT INTO serving_snapshot_table_counts VALUES (?, ?, 'logical')",
                logical_counts.items(),
            )
            conn.executemany(
                "INSERT INTO serving_snapshot_table_counts VALUES (?, ?, 'servable')",
                servable_counts.items(),
            )
            conn.commit()
        return {
            "physical": physical_counts,
            "logical": logical_counts,
            "servable": servable_counts,
        }

    def _create_package(
        self,
        root: Path,
        *,
        member: str = COMPACT_SNAPSHOT_NAME,
        compression: int = zipfile.ZIP_DEFLATED,
        extra_member: bool = False,
    ) -> tuple[Path, Path, Path, dict]:
        database_path = root / "source.db"
        counts = self._create_database(database_path)
        archive_path = root / COMPACT_ARCHIVE_NAME
        with zipfile.ZipFile(archive_path, "w", compression=compression) as archive:
            archive.write(database_path, arcname=member)
            if extra_member:
                archive.writestr("extra.txt", "unexpected")
        manifest = {
            "schema": COMPACT_MANIFEST_SCHEMA,
            "archive_member": COMPACT_SNAPSHOT_NAME,
            "database_schema": COMPACT_DATABASE_SCHEMA,
            "codec": COMPACT_POSTING_CODEC,
            "sqlite_bytes": database_path.stat().st_size,
            "sqlite_sha256": hashlib.sha256(database_path.read_bytes()).hexdigest(),
            "physical_counts": counts["physical"],
            "logical_counts": counts["logical"],
            "servable_counts": counts["servable"],
        }
        manifest_path = root / COMPACT_MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        return database_path, archive_path, manifest_path, manifest

    def test_standard_function_limit_is_strictly_below_480_million_bytes(self) -> None:
        self.assertEqual(MAX_SNAPSHOT_BYTES, 480_000_000)
        self.assertEqual(MAX_BUNDLED_DB_BYTES, MAX_SNAPSHOT_BYTES)

    def test_packaging_command_emits_one_member_archive_and_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "source.db"
            self._create_database(database)
            archive = root / "package" / COMPACT_ARCHIVE_NAME
            manifest = root / "package" / COMPACT_MANIFEST_NAME

            result = package_compact_snapshot(database, archive, manifest)

            self.assertTrue(result["ok"])
            self.assertEqual(result["sqlite_bytes"], database.stat().st_size)
            loaded, member = inspect_compact_archive(archive, manifest)
            self.assertEqual(member.filename, COMPACT_SNAPSHOT_NAME)
            self.assertEqual(loaded["logical_counts"]["ontology_concept_relations"], 2)
            self.assertEqual(loaded["servable_counts"]["ksa_items"], 1)
            destination = root / "runtime" / COMPACT_SNAPSHOT_NAME
            self.assertTrue(
                materialize_compact_snapshot(archive, manifest, destination)
            )

    def test_canonical_staging_root_and_allowlists_are_safe(self) -> None:
        expected_root = REPOSITORY_ROOT / "deploy" / "vercel_mcp_app"
        self.assertEqual(PACKAGE_DEPLOY_ROOT, expected_root)
        self.assertEqual(VERIFY_DEPLOY_ROOT, expected_root)
        self.assertEqual(
            (REPOSITORY_ROOT / ".vercelignore").read_text(encoding="utf-8"),
            (expected_root / ".vercelignore").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            json.loads((REPOSITORY_ROOT / "vercel.json").read_text("utf-8")),
            json.loads((expected_root / "vercel.json").read_text("utf-8")),
        )

        for deployment_root in (REPOSITORY_ROOT, expected_root):
            rules = (deployment_root / ".vercelignore").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(rules[0], "/*")
            self.assertIn("!api/ncs_ontology_compact.zip", rules)
            self.assertIn("!api/ncs_ontology_compact.manifest.json", rules)
            self.assertIn("api/*.db", rules)
            self.assertIn("api/**/*.db", rules)
            self.assertIn("api/**/*.db-*", rules)
            self.assertIn("!src/**/*.py", rules)
            self.assertNotIn("!deploy/", rules)
            self.assertNotIn("!data/", rules)

            config = json.loads((deployment_root / "vercel.json").read_text("utf-8"))
            self.assertIs(config["git"]["deploymentEnabled"], False)
            self.assertIn(
                "ncs_unit_job_base_links",
                config["env"]["NCS_MCP_READINESS_EXTRA_TABLES"].split(","),
            )
            self.assertIn(
                "ncs_qualification_items",
                config["env"]["NCS_MCP_READINESS_EXTRA_TABLES"].split(","),
            )
            self.assertIn(
                "ncs_unit_qualification_links",
                config["env"]["NCS_MCP_READINESS_EXTRA_TABLES"].split(","),
            )
            minimum_rows = json.loads(config["env"]["NCS_MCP_READINESS_MIN_ROWS"])
            self.assertEqual(minimum_rows["ncs_qualification_items"], 1)
            self.assertEqual(minimum_rows["ncs_unit_qualification_links"], 1)
            self.assertNotIn("builds", config)
            function = config["functions"]["api/**/*.py"]
            self.assertIn("api/ncs_ontology_compact.zip", function["includeFiles"])
            self.assertIn(
                "api/ncs_ontology_compact.manifest.json", function["includeFiles"]
            )
            self.assertIn("data/**", function["excludeFiles"])
            self.assertIn("tests/**", function["excludeFiles"])
            self.assertIn("api/**/*.db", function["excludeFiles"])
            if deployment_root == expected_root:
                self.assertLess(
                    rules.index("!api/ncs_ontology_compact.manifest.json"),
                    rules.index("api/**/*.db"),
                )

    def test_verifier_requires_an_under_limit_assembled_function_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, archive, manifest, _ = self._create_package(root)
            bundle = root / ".vercel" / "output" / "functions" / "python.func"
            self._write_function_file_map(
                root,
                bundle,
                archive_bytes=archive.read_bytes(),
                manifest_bytes=manifest.read_bytes(),
            )

            result = verify_package(
                archive,
                manifest,
                function_bundle_path=bundle,
                require_function_bundle=True,
            )

            self.assertTrue(result["ok"])
            self.assertTrue(result["function_bundle"]["checked"])
            self.assertTrue(result["function_bundle"]["required"])
            self.assertTrue(result["function_bundle"]["file_path_map_checked"])
            self.assertEqual(result["function_bundle"]["mapped_file_count"], 2)
            self.assertGreater(result["function_bundle"]["mapped_bytes"], 0)

    def test_function_bundle_gate_rejects_database_file_path_map(self) -> None:
        for database_name in (
            "unexpected.db",
            "unexpected.db-backup",
            "unexpected.sqlite",
            "unexpected.sqlite-wal",
            "unexpected.sqlite3-copy",
        ):
            with self.subTest(database_name=database_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    bundle = (
                        root / ".vercel" / "output" / "functions" / "python.func"
                    )
                    logical_path = f"api/{database_name}"
                    self._write_function_file_map(
                        root,
                        bundle,
                        extra_mappings={
                            logical_path: (logical_path, b"sqlite payload")
                        },
                    )

                    with self.assertRaisesRegex(ValueError, "forbidden database"):
                        measure_function_bundle(bundle)

    def test_function_bundle_gate_rejects_forbidden_source_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle = root / ".vercel" / "output" / "functions" / "python.func"
            self._write_function_file_map(
                root,
                bundle,
                extra_mappings={
                    "data/processed/leak.txt": (
                        "data/processed/leak.txt",
                        b"leak = True\n",
                    )
                },
            )

            with self.assertRaisesRegex(ValueError, "forbidden source path"):
                measure_function_bundle(bundle)

    def test_function_bundle_gate_rejects_database_target_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle = root / ".vercel" / "output" / "functions" / "python.func"
            self._write_function_file_map(
                root,
                bundle,
                extra_mappings={
                    "api/apparently-safe.bin": (
                        "api/hidden.db-backup",
                        b"sqlite payload",
                    )
                },
            )

            with self.assertRaisesRegex(ValueError, "forbidden database"):
                measure_function_bundle(bundle)

    def test_function_bundle_gate_counts_resolved_file_path_map_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle = root / ".vercel" / "output" / "functions" / "python.func"
            self._write_function_file_map(
                root,
                bundle,
                archive_bytes=b"z" * 101,
                manifest_bytes=b"m" * 17,
                extra_mappings={
                    "api/duplicate-archive.zip": (
                        f"api/{COMPACT_ARCHIVE_NAME}",
                        b"z" * 101,
                    )
                },
            )

            result = measure_function_bundle(bundle)

            self.assertEqual(result["mapped_file_count"], 3)
            self.assertEqual(result["unique_mapped_file_count"], 2)
            self.assertEqual(result["mapped_bytes"], 118)
            self.assertEqual(
                result["bytes"], result["physical_bytes"] + result["mapped_bytes"]
            )
            with self.assertRaisesRegex(ValueError, "filePathMap resolution"):
                measure_function_bundle(bundle, max_bytes=result["bytes"])

    def test_package_verifier_rejects_missing_required_qualification_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, archive, manifest, _ = self._create_package(root)
            (root / "vercel.json").write_text(
                json.dumps(
                    {
                        "env": {
                            "NCS_MCP_READINESS_EXTRA_TABLES": (
                                "ncs_qualification_items,ncs_unit_qualification_links"
                            ),
                            "NCS_MCP_READINESS_MIN_ROWS": json.dumps(
                                {
                                    "ncs_qualification_items": 1,
                                    "ncs_unit_qualification_links": 1,
                                }
                            ),
                        }
                    }
                ),
                encoding="utf-8",
            )

            result = verify_package(archive, manifest)

            self.assertFalse(result["ok"])
            self.assertEqual(
                result["readiness_contract"]["minimum_rows"],
                {
                    "ncs_qualification_items": 1,
                    "ncs_unit_qualification_links": 1,
                },
            )

    def test_function_bundle_gate_is_strictly_below_its_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = Path(temp_dir) / "index.func"
            bundle.mkdir()
            (bundle / "payload.bin").write_bytes(b"x" * 100)

            with self.assertRaisesRegex(ValueError, "exceeds"):
                measure_function_bundle(bundle, max_bytes=100)

    def test_packaging_command_rejects_physical_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "source.db"
            self._create_database(database)
            with closing(sqlite3.connect(database)) as conn:
                conn.execute(
                    """
                    UPDATE serving_snapshot_table_counts
                    SET row_count = 2
                    WHERE object_name = 'ksa_items'
                    """
                )
                conn.commit()

            with self.assertRaisesRegex(ValueError, "physical count mismatch"):
                package_compact_snapshot(
                    database,
                    root / COMPACT_ARCHIVE_NAME,
                    root / COMPACT_MANIFEST_NAME,
                )

    def test_packaging_command_rejects_servable_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "source.db"
            self._create_database(database)
            with closing(sqlite3.connect(database)) as conn:
                conn.execute(
                    """
                    UPDATE serving_snapshot_table_counts
                    SET row_count = 2
                    WHERE object_name = 'ksa_items' AND count_kind = 'servable'
                    """
                )
                conn.commit()

            with self.assertRaisesRegex(ValueError, "servable count mismatch"):
                package_compact_snapshot(
                    database,
                    root / COMPACT_ARCHIVE_NAME,
                    root / COMPACT_MANIFEST_NAME,
                )

    def test_materializes_exact_member_and_reuses_verified_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, archive, manifest, _ = self._create_package(root)
            destination = root / "runtime" / COMPACT_SNAPSHOT_NAME

            self.assertTrue(materialize_compact_snapshot(archive, manifest, destination))
            first_mtime = destination.stat().st_mtime_ns
            first_stamp_mtime = destination.with_suffix(
                destination.suffix + ".verified.json"
            ).stat().st_mtime_ns
            self.assertTrue(materialize_compact_snapshot(archive, manifest, destination))
            self.assertEqual(destination.stat().st_mtime_ns, first_mtime)
            self.assertEqual(
                destination.with_suffix(
                    destination.suffix + ".verified.json"
                ).stat().st_mtime_ns,
                first_stamp_mtime,
            )

    def test_concurrent_cold_starts_publish_one_valid_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, archive, manifest, _ = self._create_package(root)
            destination = root / "runtime" / COMPACT_SNAPSHOT_NAME

            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(
                    executor.map(
                        lambda _: materialize_compact_snapshot(
                            archive,
                            manifest,
                            destination,
                            lock_timeout_seconds=5,
                        ),
                        range(8),
                    )
                )

            self.assertEqual(results, [True] * 8)
            self.assertTrue(destination.is_file())
            self.assertFalse(list(destination.parent.glob("*.lock")))
            self.assertFalse(list(destination.parent.glob(".*.tmp")))

    def test_rejects_nested_or_unapproved_member_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, archive, manifest, _ = self._create_package(
                root, member=f"nested/{COMPACT_SNAPSHOT_NAME}"
            )
            destination = root / "runtime.db"

            self.assertFalse(materialize_compact_snapshot(archive, manifest, destination))
            self.assertFalse(destination.exists())

    def test_rejects_archive_with_extra_member(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, archive, manifest, _ = self._create_package(root, extra_member=True)
            with self.assertRaisesRegex(ValueError, "exactly one member"):
                inspect_compact_archive(archive, manifest)

    def test_rejects_uncompressed_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, archive, manifest, _ = self._create_package(
                root, compression=zipfile.ZIP_STORED
            )
            with self.assertRaisesRegex(ValueError, "DEFLATE"):
                inspect_compact_archive(archive, manifest)

    def test_rejects_high_ratio_zip_bomb_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = root / "source.db"
            payload.write_bytes(b"0" * 2_000_000)
            archive = root / COMPACT_ARCHIVE_NAME
            with zipfile.ZipFile(
                archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
            ) as output:
                output.write(payload, COMPACT_SNAPSHOT_NAME)
            manifest = {
                "schema": COMPACT_MANIFEST_SCHEMA,
                "archive_member": COMPACT_SNAPSHOT_NAME,
                "database_schema": COMPACT_DATABASE_SCHEMA,
                "codec": COMPACT_POSTING_CODEC,
                "sqlite_bytes": payload.stat().st_size,
                "sqlite_sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
                "physical_counts": {
                    "competency_units": 1,
                    "performance_criteria": 1,
                    "ksa_items": 1,
                    "ncs_training_courses": 1,
                },
                "logical_counts": {"ontology_concept_relations": 1},
            }
            sidecar = root / COMPACT_MANIFEST_NAME
            sidecar.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "compression ratio"):
                inspect_compact_archive(archive, sidecar)

    def test_rejects_corrupted_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, archive, manifest, _ = self._create_package(root)
            content = bytearray(archive.read_bytes())
            content[len(content) // 2] ^= 0xFF
            archive.write_bytes(content)

            self.assertFalse(
                materialize_compact_snapshot(
                    archive, manifest, root / COMPACT_SNAPSHOT_NAME
                )
            )

    def test_rejects_sha256_manifest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, archive, manifest_path, manifest = self._create_package(root)
            manifest["sqlite_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            self.assertFalse(
                materialize_compact_snapshot(
                    archive, manifest_path, root / COMPACT_SNAPSHOT_NAME
                )
            )

    def test_rejects_embedded_logical_count_manifest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, archive, manifest_path, manifest = self._create_package(root)
            manifest["logical_counts"]["ontology_concept_relations"] += 1
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            self.assertFalse(
                materialize_compact_snapshot(
                    archive, manifest_path, root / COMPACT_SNAPSHOT_NAME
                )
            )

    def test_invalid_cached_snapshot_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, archive, manifest, _ = self._create_package(root)
            destination = root / "runtime" / COMPACT_SNAPSHOT_NAME
            self.assertTrue(materialize_compact_snapshot(archive, manifest, destination))
            destination.write_bytes(b"partial")

            self.assertTrue(materialize_compact_snapshot(archive, manifest, destination))
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertFalse(list(destination.parent.glob(".*.tmp")))

    def test_external_database_override_is_explicit_opt_in(self) -> None:
        self.assertFalse(external_db_override_allowed({}))
        self.assertFalse(
            external_db_override_allowed(
                {"NCS_MCP_ALLOW_EXTERNAL_DB_OVERRIDE": "0", "NCS_DB_URL": "https://x"}
            )
        )
        self.assertTrue(
            external_db_override_allowed(
                {"NCS_MCP_ALLOW_EXTERNAL_DB_OVERRIDE": "true"}
            )
        )

    def test_readiness_contract_parsing_remains_strict(self) -> None:
        tables = readiness_required_tables(
            {
                "NCS_MCP_READINESS_EXTRA_TABLES": (
                    "ontology_concepts,ontology_concepts,bad-name,criteria_concept_forward"
                )
            }
        )
        self.assertEqual(tables[-2:], ("ontology_concepts", "criteria_concept_forward"))
        self.assertNotIn("bad-name", tables)
        self.assertEqual(
            readiness_required_min_rows(
                {"NCS_MCP_READINESS_MIN_ROWS": '{"ksa_items":574279}'}
            ),
            {"ksa_items": 574279},
        )
        with self.assertRaises(ValueError):
            readiness_required_min_rows(
                {"NCS_MCP_READINESS_MIN_ROWS": '{"bad-name":1}'}
            )

    def test_generic_override_validator_still_checks_core_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "override.db"
            self._create_database(database_path)
            self.assertTrue(sqlite_snapshot_is_usable(database_path))
            with closing(sqlite3.connect(database_path)) as conn:
                conn.execute("DELETE FROM ncs_training_courses")
                conn.commit()
            self.assertFalse(sqlite_snapshot_is_usable(database_path))


if __name__ == "__main__":
    unittest.main()
