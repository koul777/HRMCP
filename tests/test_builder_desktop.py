import json
import tempfile
import tkinter as tk
import unittest
import queue
import threading
from pathlib import Path
from unittest.mock import patch, Mock

from ncs_mcp.builder_desktop import BuilderWindow, BuilderCancelled
from ncs_mcp.builder_session import BuilderSession
from ncs_mcp.data_builder import DataBuilder


class BuilderDesktopTests(unittest.TestCase):
    def window_stub(self, phase):
        window = BuilderWindow.__new__(BuilderWindow)
        window.root = Mock()
        window.busy = True
        window.closing = False
        window.active_phase = phase
        window.cancel_requested = threading.Event()
        window.events = queue.Queue()
        return window

    def test_close_requests_cooperative_cancel_and_unwinds_lock(self):
        window = self.window_stub(2)
        with patch('ncs_mcp.builder_desktop.messagebox.askyesno', return_value=True):
            window.close()
        window.root.withdraw.assert_called_once()
        with tempfile.TemporaryDirectory() as tmp:
            engine = DataBuilder(Path(tmp), progress=window.report_progress)
            with self.assertRaises(BuilderCancelled):
                with engine.exclusive():
                    engine.progress('next page')
            self.assertFalse((engine.state / 'operation.lock').exists())
        self.assertTrue(window.events.empty())

    def test_deployment_close_keeps_verification_running(self):
        window = self.window_stub(4)
        with patch('ncs_mcp.builder_desktop.messagebox.askyesno', return_value=True):
            window.close()
        self.assertFalse(window.cancel_requested.is_set())
        window.report_progress('verify production')
        self.assertEqual(window.events.get_nowait()[0], 'progress')

    def test_declining_close_keeps_window_and_work(self):
        window = self.window_stub(1)
        with patch('ncs_mcp.builder_desktop.messagebox.askyesno', return_value=False):
            window.close()
        self.assertFalse(window.cancel_requested.is_set())
        window.root.withdraw.assert_not_called()

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
