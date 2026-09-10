from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "export_gold_lpg.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import export_gold_lpg as exporter  # noqa: E402


class ExportGoldLpgCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "fixture.db"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                """
                CREATE TABLE classifications (
                  classification_id INTEGER PRIMARY KEY,
                  major_code TEXT,
                  major_name TEXT,
                  middle_code TEXT,
                  middle_name TEXT,
                  small_code TEXT,
                  small_name TEXT,
                  sub_code TEXT,
                  sub_name TEXT,
                  review_status TEXT
                );
                CREATE TABLE ksa_items (
                  ksa_id INTEGER PRIMARY KEY,
                  ksa_text_raw TEXT NOT NULL
                );
                INSERT INTO classifications VALUES (
                  1, '02', '경영·회계·사무', '01', '기획사무',
                  '01', '경영기획', '01', '경영기획', 'raw'
                );
                INSERT INTO ksa_items VALUES (1, '변경해서는 안 되는 원천 KSA');
                """
            )
            connection.commit()
        self.source_hash = self._sha256(self.database)

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _run(self, *arguments: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *(str(argument) for argument in arguments)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def test_default_is_count_only_dry_run_with_no_artifact(self) -> None:
        before_names = sorted(path.name for path in self.root.iterdir())

        result = self._run("--db", self.database)

        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        self.assertEqual(manifest["schema"], "ncs_gold_projection_readiness_v1")
        self.assertTrue(manifest["read_only"])
        self.assertFalse(manifest["db_writes"])
        self.assertTrue(manifest["in_memory_export_allowed"])
        self.assertNotIn("projection_manifest", manifest)
        self.assertEqual(before_names, sorted(path.name for path in self.root.iterdir()))
        self.assertEqual(self.source_hash, self._sha256(self.database))

    def test_default_and_manifest_only_preflight_never_call_projection_builder(self) -> None:
        manifest_path = self.root / "readiness.json"

        with mock.patch.object(
            exporter,
            "build_gold_lpg_projection",
            side_effect=AssertionError("projection builder must not run"),
        ) as builder:
            default_summary = exporter.export_gold_lpg(self.database)
            manifest_summary = exporter.export_gold_lpg(
                self.database,
                manifest_path=manifest_path,
            )

        builder.assert_not_called()
        self.assertEqual(default_summary, manifest_summary)
        self.assertEqual(
            manifest_summary,
            json.loads(manifest_path.read_text(encoding="utf-8")),
        )
        self.assertNotIn("projection_manifest", manifest_summary)
        self.assertEqual(self.source_hash, self._sha256(self.database))

    def test_explicit_output_and_manifest_are_deterministic(self) -> None:
        projection_path = self.root / "nested" / "gold.json"
        second_projection_path = self.root / "nested" / "gold-second.json"
        first_manifest_path = self.root / "first.manifest.json"
        second_manifest_path = self.root / "second.manifest.json"

        first = self._run(
            "--db", self.database,
            "--out", projection_path,
            "--manifest-out", first_manifest_path,
        )
        second = self._run(
            "--db", self.database,
            "--out", second_projection_path,
            "--manifest-out", second_manifest_path,
        )

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        projection = json.loads(projection_path.read_text(encoding="utf-8"))
        manifest = json.loads(first_manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(projection["manifest"], manifest["projection_manifest"])
        self.assertEqual(json.loads(first.stdout), manifest)
        self.assertEqual(first_manifest_path.read_bytes(), second_manifest_path.read_bytes())
        self.assertEqual(json.loads(second.stdout), manifest)
        self.assertGreater(len(projection["nodes"]), 0)
        self.assertFalse(any(self.root.rglob("*.tmp")))
        self.assertEqual(self.source_hash, self._sha256(self.database))

    def test_preflight_and_builder_receive_the_exact_same_serving_core_profile(self) -> None:
        output = self.root / "serving-core.json"

        with mock.patch.object(
            exporter,
            "preflight_gold_projection",
            wraps=exporter.preflight_gold_projection,
        ) as preflight, mock.patch.object(
            exporter,
            "build_gold_lpg_projection",
            wraps=exporter.build_gold_lpg_projection,
        ) as builder:
            summary = exporter.export_gold_lpg(self.database, output_path=output)

        self.assertIs(
            preflight.call_args.kwargs["profile"],
            exporter.SERVING_CORE_PROFILE,
        )
        self.assertIs(
            builder.call_args.kwargs["profile"],
            exporter.SERVING_CORE_PROFILE,
        )
        self.assertIs(
            preflight.call_args.kwargs["profile"],
            builder.call_args.kwargs["profile"],
        )
        self.assertEqual(summary["profile"]["name"], "serving_core")
        projection = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(projection["manifest"]["profile"]["name"], "serving_core")
        self.assertEqual(self.source_hash, self._sha256(self.database))

    def test_high_volume_preflight_refuses_serving_core_output_without_building(self) -> None:
        high_volume_db = self.root / "high-volume.db"
        with closing(sqlite3.connect(high_volume_db)) as connection:
            connection.executescript(
                """
                CREATE TABLE classifications (
                  classification_id INTEGER PRIMARY KEY,
                  major_code TEXT,
                  major_name TEXT,
                  middle_code TEXT,
                  middle_name TEXT,
                  small_code TEXT,
                  small_name TEXT,
                  sub_code TEXT,
                  sub_name TEXT,
                  review_status TEXT
                );
                CREATE TABLE performance_criteria (
                  criteria_id INTEGER PRIMARY KEY,
                  element_id INTEGER
                );
                CREATE TABLE competency_units (
                  unit_code TEXT PRIMARY KEY,
                  classification_id INTEGER
                );
                CREATE TABLE competency_elements (
                  element_id INTEGER PRIMARY KEY,
                  unit_code TEXT
                );
                CREATE TABLE ontology_concepts (
                  concept_id INTEGER PRIMARY KEY,
                  concept_name TEXT,
                  concept_type TEXT
                );
                CREATE TABLE criteria_concept_links (
                  link_id INTEGER PRIMARY KEY,
                  criteria_id INTEGER,
                  concept_id INTEGER
                );
                INSERT INTO classifications VALUES (
                  1, '02', 'major', '01', 'middle',
                  '01', 'small', '01', 'sub', 'raw'
                );
                INSERT INTO competency_units VALUES ('U1', 1);
                INSERT INTO competency_elements VALUES (1, 'U1');
                INSERT INTO performance_criteria VALUES (1, 1);
                INSERT INTO ontology_concepts VALUES (1, 'Planning knowledge', 'knowledge');
                """
            )
            connection.executemany(
                "INSERT INTO criteria_concept_links VALUES (?, 1, 1)",
                ((identifier,) for identifier in range(1, 90001)),
            )
            connection.commit()
        before_hash = self._sha256(high_volume_db)
        output = self.root / "must-not-exist.json"
        manifest = self.root / "must-not-exist.manifest.json"

        readiness = exporter.preflight_gold_projection(
            high_volume_db,
            profile=exporter.SERVING_CORE_PROFILE,
        )
        self.assertEqual(readiness["profile"]["name"], "serving_core")
        self.assertEqual(
            readiness["table_counts"]["criteria_concept_links"]["row_count"],
            90000,
        )
        self.assertGreater(
            readiness["estimates"]["estimated_peak_in_memory_bytes"],
            readiness["thresholds"]["warning_in_memory_bytes"],
        )
        self.assertFalse(readiness["in_memory_export_allowed"])
        self.assertTrue(readiness["recommended_execution"]["streaming_required"])

        with mock.patch.object(exporter, "build_gold_lpg_projection") as builder:
            with self.assertRaisesRegex(
                exporter.StreamingRequiredError,
                "streaming_required",
            ):
                exporter.export_gold_lpg(
                    high_volume_db,
                    output_path=output,
                    manifest_path=manifest,
                )

        builder.assert_not_called()
        self.assertFalse(output.exists())
        self.assertFalse(manifest.exists())
        result = self._run(
            "--db", high_volume_db,
            "--out", output,
            "--manifest-out", manifest,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("streaming_required", result.stderr)
        self.assertEqual("", result.stdout)
        self.assertFalse(output.exists())
        self.assertFalse(manifest.exists())
        self.assertEqual(before_hash, self._sha256(high_volume_db))

    def test_rejects_source_database_as_output(self) -> None:
        result = self._run("--db", self.database, "--out", self.database)

        self.assertEqual(result.returncode, 1)
        self.assertIn("must not overwrite the source SQLite database or its sidecars", result.stderr)
        self.assertEqual("", result.stdout)
        self.assertEqual(self.source_hash, self._sha256(self.database))

    def test_rejects_every_sqlite_sidecar_for_both_destination_arguments(self) -> None:
        sidecars = {
            suffix: Path(f"{self.database}{suffix}")
            for suffix in ("-wal", "-shm", "-journal")
        }
        for suffix, path in sidecars.items():
            path.write_bytes(f"sentinel:{suffix}".encode("ascii"))
        sidecar_bytes = {suffix: path.read_bytes() for suffix, path in sidecars.items()}

        for argument in ("--out", "--manifest-out"):
            for suffix, path in sidecars.items():
                with self.subTest(argument=argument, suffix=suffix):
                    result = self._run("--db", self.database, argument, path)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(
                        "must not overwrite the source SQLite database or its sidecars",
                        result.stderr,
                    )
                    self.assertEqual("", result.stdout)
                    self.assertEqual(self.source_hash, self._sha256(self.database))
                    self.assertEqual(
                        sidecar_bytes,
                        {name: item.read_bytes() for name, item in sidecars.items()},
                    )

    def test_missing_database_has_clear_error(self) -> None:
        missing = self.root / "missing.db"

        result = self._run("--db", missing)

        self.assertEqual(result.returncode, 1)
        self.assertIn("SQLite database does not exist or is not a file", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
