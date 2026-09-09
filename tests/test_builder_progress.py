import json
import tempfile
import unittest
from pathlib import Path

from ncs_mcp.builder_progress import describe_progress, workflow_percent
from ncs_mcp.builder_discovery import discover_project


class BuilderProgressTests(unittest.TestCase):
    def test_measured_and_unknown_work(self):
        self.assertEqual(describe_progress(dict(stage='copy', completed=5, total=20))[1], 25)
        for total in (None, 0, float('nan'), 4):
            self.assertIsNone(describe_progress(dict(completed=5, total=total))[1])
        self.assertIsNone(describe_progress('SQL')[1])

    def test_failed_and_running_are_not_completed(self):
        self.assertEqual(workflow_percent({1:'done', 2:'failed', 3:'running', 4:'pending'}), (25, 1))

    def test_discovery_uses_mcp_link_not_repository_root_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'deploy/vercel_mcp_app'
            (folder / '.vercel').mkdir(parents=True)
            (folder / '.vercel/project.json').write_text(json.dumps(dict(projectId='prj_x', orgId='team_x', projectName='test-mcp')))
            (folder / 'vercel.json').write_text('{}')
            state = root / '.state'
            state.mkdir()
            (state / 'deployed.json').write_text('{broken')
            result = discover_project(root, state)
            self.assertEqual(result['production_mcp_url'], 'https://test-mcp.vercel.app/api/mcp')
            (folder / '.vercel/project.json').write_text('{}')
            self.assertIsNone(discover_project(root, state))
