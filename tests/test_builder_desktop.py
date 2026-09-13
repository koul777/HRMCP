import json
import tempfile
import tkinter as tk
import unittest
import queue
import threading
from pathlib import Path
from unittest.mock import patch, Mock

from ncs_mcp.builder_desktop import BuilderWindow, BuilderCancelled, capacity_for_version, format_result
from ncs_mcp.builder_release import snapshot_capacity_message
from ncs_mcp.builder_session import BuilderSession
from ncs_mcp.data_builder import DataBuilder


class BuilderDesktopTests(unittest.TestCase):
    @staticmethod
    def capacity(version='a1', status='soft_cap_warning'):
        size = 480_000_000 if status == 'hard_cap_exceeded' else 478_756_864
        return {'version': version, 'status': status, 'database_bytes': size,
                'soft_cap_bytes': 460_000_000, 'hard_cap_bytes': 480_000_000,
                'soft_headroom_bytes': 460_000_000 - size,
                'hard_headroom_bytes': 480_000_000 - size}

    def test_capacity_result_shows_numeric_warning_for_package_and_deploy(self):
        for key in ('package', 'deployment'):
            text = format_result({'version': 'a1', key: {'ok': True, 'snapshot_capacity': self.capacity()}})
            self.assertIn('478,756,864', text)
            self.assertIn('1,243,136', text)
            self.assertIn('경고', text)
            self.assertIn('다른 검증을 통과하면 배포', text)
        self.assertNotIn('경량 DB 용량', format_result({'version': 'a1', 'sources': ['training-courses']}))

    def test_capacity_ui_rejects_other_version_and_missing_measurement(self):
        release = {'snapshot_capacity': self.capacity(version='other')}
        self.assertIsNone(capacity_for_version(release, 'a1'))
        self.assertIn('미측정', format_result({'version': 'a1', 'package': release}))
        self.assertIn('미측정', snapshot_capacity_message({'database_bytes': 'untrusted'}))
        self.assertIsNone(capacity_for_version({'snapshot_capacity': {}}, None))

    def test_reselected_version_restores_soft_warning_or_hard_block_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = DataBuilder(Path(tmp))
            folder = engine._version_dir('a1')
            folder.mkdir(parents=True)
            (folder / 'build.json').write_text('{"status":"ready"}')
            window = self.window_stub(3)
            window.engine = engine
            window.capacity_status = Mock()
            window.phase_states = {}
            window.phase_labels = {number: Mock() for number in range(1, 5)}
            window.update_overall = Mock()
            for status, release_status, completed in (
                    ('soft_cap_warning', 'package_ready', True),
                    ('hard_cap_exceeded', 'build_failed', False)):
                (folder / 'release.json').write_text(json.dumps({
                    'status': release_status, 'package_validated': completed,
                    'snapshot_capacity': self.capacity(status=status)}))
                window.restore_phase_states('a1')
                text = window.capacity_status.set.call_args.args[0]
                self.assertIn('경고' if completed else '차단', text)
                self.assertEqual(window.phase_states[3], 'done' if completed else 'pending')

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
