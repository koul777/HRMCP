import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from ncs_mcp.builder_desktop import BuilderWindow
from ncs_mcp.builder_session import BuilderSession
from ncs_mcp.data_builder import DataBuilder


class BuilderDesktopTests(unittest.TestCase):
    def test_reopen_restores_completed_version_and_package_after_deploy_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = DataBuilder(Path(tmp))
            version = '20260909_123456_abcd'
            folder = engine._version_dir(version)
            folder.mkdir(parents=True)
            (folder / 'build.json').write_text(json.dumps(dict(version=version, kind='api', status='ready', source_delta={'counts':{}}, sources=['training-courses'])))
            (folder / 'release.json').write_text(json.dumps(dict(status='deploy_failed', package_validated=True)))
            journal = BuilderSession(engine.state)
            journal.start(3, version)
            journal.finish(version)
            journal.start(4, version)
            journal.fail('connection failed')
            root = tk.Tk()
            root.withdraw()
            try:
                with patch('ncs_mcp.builder_desktop.DataBuilder', return_value=engine):
                    window = BuilderWindow(root)
                self.assertEqual(window.selected_version, version)
                self.assertEqual(window.phase_states, {1:'done', 2:'done', 3:'done', 4:'pending'})
                self.assertIn('75%', window.overall_label.get())
                self.assertFalse(window.busy)
            finally:
                root.destroy()
