import json
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ncs_mcp.builder_release import (
    ReleaseError, _artifact_evidence, _run_with_progress, _stage_artifacts,
    _tree_evidence, deploy_release,
)
from ncs_mcp.data_builder import BuilderError, DataBuilder
from scripts import build_vercel_snapshot as snapshot


class ReleaseProgressTests(unittest.TestCase):
    @staticmethod
    def _builder_version(base: Path):
        builder = DataBuilder(base)
        version = "a1"
        root = builder.state / "versions" / version
        root.mkdir(parents=True, exist_ok=True)
        return builder, version, root

    @staticmethod
    def _deploy(builder, version, root, **kwargs):
        with builder.exclusive("deploy", version) as builder_context:
            return deploy_release(root, builder_context=builder_context, **kwargs)

    def _retry_package(self, root):
        stage = root / 'release/deploy'
        stage.mkdir(parents=True)
        source = root / 'ncs.db'
        source.write_bytes(b'candidate')
        artifact = stage / 'archive.zip'
        artifact.write_bytes(b'validated package')
        url = 'https://selected.vercel.app/api/mcp'
        project_dir = stage / '.vercel'
        project_dir.mkdir()
        (project_dir / 'project.json').write_text(json.dumps({
            'projectId': 'prj_selected', 'orgId': 'team_selected',
            'projectName': 'selected',
        }))
        bundle = project_dir / 'output/functions/python.func'
        bundle.mkdir(parents=True)
        (bundle / '.vc-config.json').write_text('{}')
        bundle_report = root / 'release/function-bundle-verification.json'
        bundle_report.write_text('{"ok":true}')
        (root / 'release.json').write_text(json.dumps({
            'status': 'package_ready', 'ok': True, 'package_validated': True,
            'stage_dir': str(stage), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'artifacts': _stage_artifacts(stage),
            'prebuilt_output': _tree_evidence(project_dir / 'output'),
            'function_bundle_verification': {
                'sha256': _artifact_evidence(bundle_report)['sha256'],
                'function_bundle_path': str(bundle), 'exact_paths_verified': True,
            },
            'repo_root': str(root.parents[3]), 'build_id': 'id', 'project': {
                'projectId': 'prj_selected', 'orgId': 'team_selected',
                'projectName': 'selected', 'production_mcp_url': url,
            }}))
        return url, artifact

    @staticmethod
    def _release_runner():
        state = {'production': 'https://selected-old.vercel.app'}
        ids = {
            'https://selected-old.vercel.app': 'dpl_old',
            'https://selected-new.vercel.app': 'dpl_new',
        }

        def run(argv, _cwd):
            if len(argv) > 1 and argv[1] == 'inspect':
                current = state['production']
                return f'id {ids[current]}\nurl {current}\nname selected'
            if len(argv) > 1 and argv[1] == 'deploy':
                return 'https://selected-new.vercel.app'
            if len(argv) > 1 and argv[1] == 'promote':
                state['production'] = argv[2]
            if len(argv) > 1 and argv[1] == 'rollback':
                state['production'] = argv[2]
            return ''

        return run

    def test_failed_staging_retry_reuses_package_and_preserves_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            builder, version, root = self._builder_version(Path(folder))
            url, artifact = self._retry_package(root)
            before = artifact.read_bytes()
            run = self._release_runner()
            failed = self._deploy(
                builder, version, root, production_mcp_url=url, runner=run,
                verifier=lambda *_: {'ok': False},
            )
            self.assertTrue(failed['package_validated'])
            success = self._deploy(
                builder, version, root, production_mcp_url=url, runner=run,
                verifier=lambda *_: {'ok': True},
            )
            self.assertTrue(success['ok'])
            self.assertNotIn('failed_phase', success)
            self.assertNotIn('error', success)
            self.assertEqual(success['deployment_attempts'][0]['failed_phase'], 'staged')
            self.assertEqual(success['deployment_attempts'][1]['status'], 'deployed')
            self.assertEqual(artifact.read_bytes(), before)

    def test_failed_deployment_retry_rechecks_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            builder, version, root = self._builder_version(Path(folder))
            url, artifact = self._retry_package(root)
            self._deploy(
                builder, version, root, production_mcp_url=url,
                runner=self._release_runner(), verifier=lambda *_: {'ok': False},
            )
            artifact.write_bytes(b'tampered')
            with patch('ncs_mcp.builder_release._run') as run:
                result = self._deploy(
                    builder, version, root, production_mcp_url=url,
                )
            run.assert_not_called()
            self.assertEqual(result['failed_phase'], 'preflight')
            self.assertIn('package changed', result['error'])

    def test_retry_does_not_reuse_stale_promotion_state(self):
        with tempfile.TemporaryDirectory() as folder:
            builder, version, root = self._builder_version(Path(folder))
            url, _ = self._retry_package(root)
            verification = iter(({'ok': True}, {'ok': False}))
            run = self._release_runner()
            failed = self._deploy(
                builder, version, root, production_mcp_url=url,
                runner=run, verifier=lambda *_: next(verification),
            )
            self.assertTrue(failed['promotion_performed'])
            retry = self._deploy(
                builder, version, root, production_mcp_url=url,
                runner=run, verifier=lambda *_: {'ok': False},
            )
            self.assertNotIn('promotion_performed', retry)
            self.assertNotIn('production_verification', retry)
            self.assertTrue(retry['deployment_attempts'][0]['promotion_performed'])

    def test_failed_package_without_validation_marker_cannot_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            builder, version, root = self._builder_version(Path(folder))
            url, _ = self._retry_package(root)
            path = root / 'release.json'
            report = json.loads(path.read_text())
            report.update(status='deploy_failed', ok=False)
            report.pop('package_validated')
            path.write_text(json.dumps(report))
            with patch('ncs_mcp.builder_release._run') as run:
                result = self._deploy(
                    builder, version, root, production_mcp_url=url,
                )
            run.assert_not_called()
            self.assertFalse(result['ok'])

    def test_relay_delivers_last_record_even_for_fast_runner(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'progress.json'
            event = {'stage': '검증 완료', 'completed': 3, 'total': 3, 'unit': '공정 완료'}
            events = []
            def run(*_):
                path.write_text(json.dumps(event), encoding='utf-8')
                return 'done'
            self.assertEqual(_run_with_progress(run, [], Path(folder), path, events.append), 'done')
            self.assertEqual(events, [event])

    def test_failed_runner_stops_observer_and_rejects_invalid_record(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'progress.json'
            path.write_text('{"stage":"bad","completed":99}', encoding='utf-8')
            before = set(threading.enumerate())
            events = []
            def fail(*_):
                raise ReleaseError('failed')
            with self.assertRaises(ReleaseError):
                _run_with_progress(fail, [], Path(folder), path, events.append)
            self.assertEqual(events, [])
            self.assertEqual(set(threading.enumerate()), before)

    def test_snapshot_stage_counts_and_failure_never_claim_completion(self):
        for failed_stage in (None, 'package_compact_snapshot'):
            with self.subTest(failed_stage=failed_stage), tempfile.TemporaryDirectory() as folder:
                builder, version, root = self._builder_version(Path(folder))
                source = root / 'ncs.db'
                source.write_bytes(snapshot.SQLITE_HEADER + b'source')
                release = root / 'release'
                release.mkdir()
                progress = release / 'progress.json'
                events = []
                def run(stage, *, builder_context):
                    events.append(json.loads(progress.read_text(encoding='utf-8')))
                    failed = stage['name'] == failed_stage
                    if not failed:
                        for name in stage['required_artifacts']:
                            Path(name).write_bytes(snapshot.SQLITE_HEADER + b'output')
                    return {**stage, 'returncode': int(failed)}
                with builder.exclusive('package', version) as builder_context, \
                        patch.object(snapshot, '_run_stage', side_effect=run):
                    result = snapshot.build_snapshot(
                        source=source, output_db=release / 'out.db',
                        archive=release / 'out.zip', manifest=release / 'manifest.json',
                        report_path=release / 'report.json', progress_file=progress,
                        builder_context=builder_context,
                    )
                self.assertEqual([event['completed'] for event in events],
                                 [0, 1, 2] if failed_stage is None else [0, 1])
                final = json.loads(progress.read_text(encoding='utf-8'))
                self.assertEqual(final['completed'], 3 if failed_stage is None else 1)
                self.assertEqual(result['ok'], failed_stage is None)

    def test_progress_path_cannot_overwrite_source(self):
        with tempfile.TemporaryDirectory() as folder:
            builder, version, root = self._builder_version(Path(folder))
            source = root / 'ncs.db'
            original = snapshot.SQLITE_HEADER + b'source'
            source.write_bytes(original)
            release = root / 'release'
            release.mkdir()
            with self.assertRaises(BuilderError), \
                    builder.exclusive('package', version) as builder_context:
                snapshot.build_snapshot(
                    source=source, output_db=release / 'out.db',
                    archive=release / 'out.zip', manifest=release / 'manifest.json',
                    report_path=release / 'report.json', progress_file=source,
                    builder_context=builder_context,
                )
            self.assertEqual(source.read_bytes(), original)

    def test_dry_run_does_not_write_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.db'
            source.write_bytes(snapshot.SQLITE_HEADER + b'source')
            progress = root / 'progress.json'
            result = snapshot.build_snapshot(source=source, output_db=root / 'out.db',
                archive=root / 'out.zip', manifest=root / 'manifest.json',
                report_path=root / 'report.json', progress_file=progress, dry_run=True)
            self.assertTrue(result['ok'])
            self.assertFalse(progress.exists())

    def test_deployment_counts_stop_at_failed_verification(self):
        # Package preparation and transport are replaced; this never deploys.
        for success in (False, True):
            with self.subTest(success=success), tempfile.TemporaryDirectory() as folder:
                builder, version, root = self._builder_version(Path(folder))
                url, _ = self._retry_package(root)
                events = []
                result = self._deploy(
                    builder, version, root, production_mcp_url=url,
                    progress=events.append, runner=self._release_runner(),
                    verifier=lambda *_: {'ok': success},
                )
                counts = [event['completed'] for event in events if event['total'] == 5]
                self.assertEqual(counts, [0, 1, 2, 3, 4, 5] if success else [0, 1, 2])
                self.assertEqual(result['ok'], success)
