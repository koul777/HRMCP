import json
from pathlib import Path
import tempfile
import unittest

from ncs_mcp.builder_session import BuilderSession


class BuilderSessionTests(unittest.TestCase):
    def test_success_survives_restart_and_failure_preserves_version(self):
        with tempfile.TemporaryDirectory() as directory:
            session = BuilderSession(Path(directory))
            identifier = session.start("excel")
            session.finish("verified-version")
            restored = BuilderSession(Path(directory))
            self.assertEqual(restored.data["attempts"][0]["id"], identifier)
            restored.start("api", "verified-version")
            restored.fail("sanitized error")
            self.assertEqual(BuilderSession(Path(directory)).data["selected_version"], "verified-version")

    def test_running_recovery_is_incomplete_and_retains_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            session = BuilderSession(Path(directory))
            session.start("api")
            session.progress({"stage": "API", "completed": 2, "total": 4, "unit": "페이지", "service_key": "secret"})
            restored = BuilderSession(Path(directory))
            attempt = restored.data["attempts"][0]
            self.assertEqual(attempt["status"], "incomplete")
            self.assertEqual(attempt["progress"]["completed"], 2)
            self.assertNotIn("secret", restored.path.read_text(encoding="utf-8"))
            self.assertIsNone(restored.data["selected_version"])
            self.assertEqual(json.loads(restored.path.read_text(encoding="utf-8"))["attempts"][0]["status"], "incomplete")

    def test_repeated_start_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            session = BuilderSession(Path(directory))
            session.start("api")
            with self.assertRaises(RuntimeError):
                session.start("excel")


if __name__ == "__main__":
    unittest.main()
