from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ROOT = ROOT / "deploy" / "vercel_mcp_app"

REQUIRED_SOURCE_PATHS = (
    "api/index.py",
    "api/bootstrap_runtime.py",
    "api/bootstrap_state.py",
    "api/health.py",
    "api/mcp.py",
    "api/ready.py",
    "api/ncs_ontology_compact.zip",
    "api/ncs_ontology_compact.manifest.json",
    "src/ncs_mcp/server.py",
)

PROHIBITED_SOURCE_PATHS = (
    ".env",
    "api/.env",
    "api/private.key",
    "api/ncs.db",
    "api/ncs.db-wal",
    "data/raw/ncs.xlsx",
    "data/processed/ncs.db",
    "tests/test_vercel_api.py",
    "reports/ncs_search_baseline.json",
)


def _glob_matches(pattern: str, path: str) -> bool:
    pattern = pattern.lstrip("/")
    expression: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            expression.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            expression.append(".*")
            index += 2
        elif pattern[index] == "*":
            expression.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            expression.append("[^/]")
            index += 1
        else:
            expression.append(re.escape(pattern[index]))
            index += 1
    return re.fullmatch("".join(expression), path) is not None


def _is_allowlisted(path: str, rules: list[str]) -> bool:
    top_level = path.split("/", 1)[0]
    if f"!{top_level}" not in rules and f"!{path}" not in rules:
        return False

    included = False
    for rule in rules:
        if not rule or rule.startswith("#") or rule == "/*":
            continue
        negated = rule.startswith("!")
        pattern = rule[1:] if negated else rule
        if _glob_matches(pattern, path):
            included = negated
    return included


class VercelSourceUploadAllowlistTests(unittest.TestCase):
    def test_root_and_staging_allowlists_have_identical_safe_api_rules(self) -> None:
        root_rules = (ROOT / ".vercelignore").read_text(encoding="utf-8").splitlines()
        deploy_rules = (DEPLOY_ROOT / ".vercelignore").read_text(
            encoding="utf-8"
        ).splitlines()

        self.assertEqual(root_rules, deploy_rules)
        self.assertEqual(root_rules[0], "/*")
        self.assertNotIn("!api/", root_rules)
        self.assertIn("!api", root_rules)
        self.assertIn("!api/**/*.py", root_rules)
        self.assertLess(root_rules.index("!api"), root_rules.index("!api/**/*.py"))
        self.assertIn("!api/ncs_ontology_compact.zip", root_rules)
        self.assertIn("!api/ncs_ontology_compact.manifest.json", root_rules)

    def test_runtime_sources_and_compact_snapshot_are_allowlisted(self) -> None:
        for deployment_root in (ROOT, DEPLOY_ROOT):
            rules = (deployment_root / ".vercelignore").read_text(
                encoding="utf-8"
            ).splitlines()
            for path in REQUIRED_SOURCE_PATHS:
                with self.subTest(root=deployment_root.name, path=path):
                    self.assertTrue(_is_allowlisted(path, rules))

    def test_secrets_raw_data_databases_tests_and_reports_stay_excluded(self) -> None:
        for deployment_root in (ROOT, DEPLOY_ROOT):
            rules = (deployment_root / ".vercelignore").read_text(
                encoding="utf-8"
            ).splitlines()
            for path in PROHIBITED_SOURCE_PATHS:
                with self.subTest(root=deployment_root.name, path=path):
                    self.assertFalse(_is_allowlisted(path, rules))


if __name__ == "__main__":
    unittest.main()
