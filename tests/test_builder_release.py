import hashlib
import copy
import json
import os
import sqlite3
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ncs_mcp.builder_release import (
    ReleaseError, _is_canonically_within, _verify, _verify_deployment,
    _write, build_release as direct_build_release, deploy_release as direct_deploy_release,
    project_configuration,
)
from ncs_mcp.builder_authorization import BuilderAuthorizationError
from ncs_mcp.data_builder import BuilderError, DataBuilder


def build_release(version_dir, **kwargs):
    with DataBuilder(kwargs['repo_root']).exclusive('package', version_dir.name) as context:
        return direct_build_release(version_dir, builder_context=context, **kwargs)


def deploy_release(version_dir, **kwargs):
    root = version_dir.parent.parent.parent.parent
    with DataBuilder(root).exclusive('deploy', version_dir.name) as context:
        return direct_deploy_release(version_dir, builder_context=context, **kwargs)


class BuilderReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.version = self.root / '.state/ncs-data-builder/versions/a1'
        self.version.mkdir(parents=True)
        source = self.version / 'ncs.db'
        with sqlite3.connect(source) as connection:
            for name in ('ksa_items', 'ksa_atomic_items', 'ontology_concepts',
                         'ksa_concept_links', 'ksa_atomic_concept_links'):
                connection.execute(f'CREATE TABLE {name} (id INTEGER)')
                connection.execute(f'INSERT INTO {name} VALUES (1)')
        connection.close()
        self.sha = hashlib.sha256(source.read_bytes()).hexdigest()
        self.template = self.root / 'deploy/vercel_mcp_app'
        self.files = ['api/index.py', 'vercel.json', 'requirements.txt']
        for name in self.files:
            path = self.template / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}' if name.endswith('.json') else 'code', encoding='utf-8')
        (self.template / '.vercel').mkdir()
        (self.template / '.vercel/project.json').write_text(json.dumps({
            'projectId': 'prj_test', 'orgId': 'team_test', 'projectName': 'selected-project'}))
        (self.template / '.env').write_text('SECRET=hidden')
        self.commands = []
        self.previous_deployment = 'https://selected-project-old.vercel.app'
        self.staged_deployment = 'https://selected-project-new.vercel.app'
        self.current_deployment = self.previous_deployment
        self.deployment_ids = {
            self.previous_deployment: 'dpl_previous',
            self.staged_deployment: 'dpl_staged',
        }
        def snapshot(**kwargs):
            argv = [sys.executable, 'build_vercel_snapshot.py']
            for key, flag in (('source', '--source'), ('output_db', '--output-db'),
                              ('archive', '--archive'), ('manifest', '--manifest'),
                              ('report_path', '--report')):
                argv.extend([flag, str(kwargs[key])])
            self.run_command(argv, self.root)
            return json.loads(kwargs['report_path'].read_text())
        self.enterContext(patch('scripts.build_vercel_snapshot.build_snapshot', side_effect=snapshot))

    def run_command(self, argv, cwd):
        self.commands.append((argv, cwd))
        if argv[0] == 'git':
            return '\0'.join('deploy/vercel_mcp_app/' + name for name in self.files) + '\0'
        if '--source' in argv and str(argv[1]).endswith('build_vercel_snapshot.py'):
            for flag in ('--output-db', '--archive', '--manifest'):
                Path(argv[argv.index(flag) + 1]).write_bytes(b'compact')
            Path(argv[argv.index('--report') + 1]).write_text(
                '{"ok":true,"generated_at":"2026-09-12T01:02:03+00:00"}'
            )
            return ''
        if len(argv) > 1 and argv[1] == 'build':
            bundle = cwd / '.vercel/output/functions/python.func'
            bundle.mkdir(parents=True)
            (bundle / '.vc-config.json').write_text('{"filePathMap":{"api/x":"api/x"}}')
            (bundle / 'handler.py').write_text('app = object()')
            return ''
        if str(argv[1]).endswith('verify_vercel_compact_package.py'):
            Path(argv[argv.index('--out') + 1]).write_text(json.dumps({
                'ok': True,
                'archive_path': str(Path(argv[argv.index('--archive') + 1]).resolve()),
                'manifest_path': str(Path(argv[argv.index('--manifest') + 1]).resolve()),
                'function_bundle': {
                    'checked': True, 'required': True, 'ok': True,
                    'path': str(Path(argv[argv.index('--function-bundle') + 1]).resolve()),
                    'bytes': 42,
                },
            }))
            return ''
        if len(argv) > 1 and argv[1] == 'inspect':
            return (f'id {self.deployment_ids[self.current_deployment]}\n'
                    f'url {self.current_deployment}\nname selected-project')
        if len(argv) > 1 and argv[1] == 'deploy':
            return self.staged_deployment
        if len(argv) > 1 and argv[1] == 'promote':
            self.current_deployment = argv[2]
            return ''
        if len(argv) > 1 and argv[1] == 'rollback':
            self.current_deployment = argv[2]
            return ''
        return ''

    def build(self):
        return build_release(self.version, repo_root=self.root, deploy_root=self.template,
                             expected_source_sha256=self.sha, runner=self.run_command)

    def verified(self, _url, build_id, *_args, **_kwargs):
        return {'ok': True, 'build_identity_matches': True,
                'health': {'ok': True, 'http_status': 200, 'service_status': 'ok'},
                'ready': {'ok': True, 'http_status': 200, 'service_status': 'ready'},
                'mcp': {'ok': True, 'build_identity_matches': True,
                        'server_version': '1.0+git.' + build_id}}

    def completed_deployment(self):
        self.build()
        report = deploy_release(self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=self.run_command, verifier=self.verified)
        self.assertTrue(report['ok'], report)
        return report

    def test_template_copy_target_hardlink_race_never_truncates_source(self):
        source = self.version / 'ncs.db'
        before = source.read_bytes()
        target = self.version / 'release/deploy/api/index.py'
        original_open = Path.open
        injected = []
        def racing_open(path, mode='r', *args, **kwargs):
            if mode == 'xb' and path.parent == target.parent and path.name.startswith('index.py.'):
                os.link(source, target)
                injected.append(True)
            return original_open(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', racing_open):
            with self.assertRaises(BuilderError) as rejected:
                self.build()
        self.assertIsInstance(rejected.exception.__cause__, BuilderAuthorizationError)
        self.assertEqual(injected, [True])
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse((self.version / 'release.json').exists())
        self.assertEqual([argv[0] for argv, _ in self.commands], ['git'])

    def test_deployed_report_pointer_gap_recovers_under_new_lease_without_remote_mutation(self):
        self.build()
        engine = DataBuilder(self.root)
        pointer = engine.state / 'deployed.json'
        pointer.write_text(json.dumps({'version': 'a0', 'production_url': self.previous_deployment}))
        baseline = engine.state / 'current.json'
        baseline.write_text('{"version":"a0"}')
        old_pointer, old_baseline = pointer.read_bytes(), baseline.read_bytes()
        real_write = _write
        saved_operations = []
        def lose_lease_after_success(path, value, **kwargs):
            real_write(path, value, **kwargs)
            if path == self.version / 'release.json' and value.get('status') == 'deployed':
                saved_operations.append(kwargs['builder_context'].operation_id)
                (engine.state / 'operation.lock').unlink()
        with patch.object(engine, 'candidate', return_value=self.version / 'ncs.db'), \
                patch('ncs_mcp.builder_release._run', side_effect=self.run_command), \
                patch('ncs_mcp.builder_release._verify_deployment', side_effect=self.verified), \
                patch('ncs_mcp.builder_release._write', side_effect=lose_lease_after_success):
            with self.assertRaises(BuilderError) as rejected:
                engine.deploy(self.version.name, self.template,
                              'https://selected-project.vercel.app/api/mcp')
        self.assertIsInstance(rejected.exception.__cause__, BuilderAuthorizationError)
        self.assertEqual(self.current_deployment, self.staged_deployment)
        self.assertEqual(json.loads((self.version / 'release.json').read_text())['status'], 'deployed')
        self.assertEqual(pointer.read_bytes(), old_pointer)
        count = len(self.commands)
        with patch.object(engine, 'candidate', return_value=self.version / 'ncs.db'), \
                patch('ncs_mcp.builder_release._run', side_effect=self.run_command), \
                patch('ncs_mcp.builder_release._verify_deployment', side_effect=self.verified):
            result = engine.deploy(self.version.name, self.template,
                                   'https://selected-project.vercel.app/api/mcp')
        self.assertTrue(result['deployment']['ok'], result)
        reconciliation = result['deployment']['local_reconciliation']
        self.assertFalse(reconciliation['remote_mutation_performed'])
        self.assertFalse(reconciliation['baseline_advanced'])
        current = json.loads(pointer.read_text())
        self.assertEqual(current['version'], self.version.name)
        self.assertNotIn(current['operation_lineage']['operation_id'], saved_operations)
        self.assertEqual(current['deployment_identity']['deployment_id'], 'dpl_staged')
        self.assertEqual(current['build_id'], result['deployment']['build_id'])
        self.assertEqual(current['source_sha256'], self.sha)
        self.assertEqual(baseline.read_bytes(), old_baseline)
        self.assertEqual([argv[1] for argv, _ in self.commands[count:]], ['inspect', 'inspect'])

    def test_completed_recovery_rejects_divergence_and_preserves_durable_success(self):
        self.completed_deployment()
        path = self.version / 'release.json'
        before = path.read_bytes()
        self.current_deployment = self.previous_deployment
        count = len(self.commands)
        result = deploy_release(self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=self.run_command, verifier=self.verified)
        self.assertEqual(result['status'], 'reconciliation_failed')
        self.assertIn('diverged', result['error'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([argv[1] for argv, _ in self.commands[count:]], ['inspect'])

    def test_completed_recovery_rejects_hash_mismatch_before_remote_checks(self):
        self.completed_deployment()
        path = self.version / 'release.json'
        before = path.read_bytes()
        (self.version / 'ncs.db').write_bytes(b'changed')
        count = len(self.commands)
        result = deploy_release(self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=self.run_command, verifier=self.verified)
        self.assertEqual(result['status'], 'reconciliation_failed')
        self.assertIn('source changed', result['error'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.commands), count)

    def test_completed_recovery_health_failure_is_retryable_and_requires_exact_build(self):
        report = self.completed_deployment()
        path = self.version / 'release.json'
        before = path.read_bytes()
        count = len(self.commands)
        failed = deploy_release(self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=self.run_command, verifier=lambda *_: self.verified('', 'wrong-build'))
        self.assertEqual(failed['status'], 'reconciliation_failed')
        self.assertEqual(path.read_bytes(), before)
        recovered = deploy_release(self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=self.run_command, verifier=self.verified)
        self.assertTrue(recovered['ok'], recovered)
        self.assertEqual(recovered['build_id'], report['build_id'])
        repeated = deploy_release(self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=self.run_command, verifier=self.verified)
        self.assertTrue(repeated['ok'], repeated)
        self.assertEqual(repeated['production_after_promotion'], report['production_after_promotion'])
        self.assertTrue(all(argv[1] == 'inspect' for argv, _ in self.commands[count:]))

    def test_completed_recovery_alias_change_during_verification_cannot_update_local_state(self):
        self.completed_deployment()
        path = self.version / 'release.json'
        before = path.read_bytes()
        count = len(self.commands)
        def diverge(url, build_id):
            self.current_deployment = self.previous_deployment
            return self.verified(url, build_id)
        result = deploy_release(self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=self.run_command, verifier=diverge)
        self.assertEqual(result['status'], 'reconciliation_failed')
        self.assertIn('changed during local recovery', result['error'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([argv[1] for argv, _ in self.commands[count:]], ['inspect', 'inspect'])

    def test_completed_recovery_rejects_copied_and_expired_context_before_io(self):
        self.completed_deployment()
        path = self.version / 'release.json'
        before = path.read_bytes()
        count = len(self.commands)
        engine = DataBuilder(self.root)
        with engine.exclusive('deploy', self.version.name) as context:
            for invalid in (None, copy.copy(context), context.lineage()):
                with self.assertRaises(BuilderAuthorizationError):
                    direct_deploy_release(self.version, builder_context=invalid,
                        production_mcp_url='https://selected-project.vercel.app/api/mcp',
                        runner=self.run_command, verifier=self.verified)
        with self.assertRaises(BuilderAuthorizationError):
            direct_deploy_release(self.version, builder_context=context,
                production_mcp_url='https://selected-project.vercel.app/api/mcp',
                runner=self.run_command, verifier=self.verified)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.commands), count)

    def test_completed_recovery_lost_lease_during_verification_does_not_write(self):
        self.completed_deployment()
        path = self.version / 'release.json'
        before = path.read_bytes()
        count = len(self.commands)
        def revoke(url, build_id):
            (self.root / '.state/ncs-data-builder/operation.lock').unlink()
            return self.verified(url, build_id)
        with self.assertRaises(BuilderError) as rejected:
            deploy_release(self.version,
                production_mcp_url='https://selected-project.vercel.app/api/mcp',
                runner=self.run_command, verifier=revoke)
        self.assertIsInstance(rejected.exception.__cause__, BuilderAuthorizationError)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([argv[1] for argv, _ in self.commands[count:]], ['inspect'])

    def test_direct_release_calls_require_a_live_exact_capability_before_io(self):
        arguments = dict(repo_root=self.root, deploy_root=self.template,
                         expected_source_sha256=self.sha, runner=self.run_command)
        engine = DataBuilder(self.root)
        with engine.exclusive('package', self.version.name) as context:
            for invalid in (None, context.lineage(), copy.copy(context), copy.deepcopy(context)):
                with self.subTest(invalid=type(invalid).__name__):
                    with self.assertRaises(BuilderAuthorizationError):
                        direct_build_release(self.version, builder_context=invalid, **arguments)
                    with self.assertRaises(BuilderAuthorizationError):
                        direct_deploy_release(self.version, builder_context=invalid,
                                              production_mcp_url='https://selected-project.vercel.app/api/mcp',
                                              runner=self.run_command)
            with self.assertRaises(BuilderAuthorizationError):
                direct_deploy_release(self.version, builder_context=context,
                                      production_mcp_url='https://selected-project.vercel.app/api/mcp',
                                      runner=self.run_command)
            with self.assertRaises(BuilderAuthorizationError):
                direct_build_release(self.version.with_name('a2'), builder_context=context, **arguments)
            with self.assertRaises(BuilderAuthorizationError):
                direct_build_release(self.version, builder_context=context,
                                     **{**arguments, 'repo_root': self.root / 'other'})
        with self.assertRaises(BuilderAuthorizationError):
            direct_build_release(self.version, builder_context=context, **arguments)
        self.assertEqual(self.commands, [])
        self.assertFalse((self.version / 'release.json').exists())
        self.assertFalse((self.version / 'release').exists())

    def test_legacy_fixed_temp_hardlink_never_truncates_source(self):
        source = self.version / 'ncs.db'
        before = source.read_bytes()
        os.link(source, self.version / 'release.tmp')
        with DataBuilder(self.root).exclusive('package', self.version.name) as context:
            result = direct_build_release(self.version, repo_root=self.root,
                deploy_root=self.template, expected_source_sha256='invalid',
                runner=self.run_command, builder_context=context)
        self.assertFalse(result['ok'])
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.commands, [])

    def test_report_and_random_temp_hardlinks_are_rejected_without_source_writes(self):
        source = self.version / 'ncs.db'
        before = source.read_bytes()
        output = self.version / 'release.json'
        with DataBuilder(self.root).exclusive('package', self.version.name) as context:
            os.link(source, output)
            with self.assertRaises(BuilderAuthorizationError):
                _write(output, {'overwrite': True}, builder_context=context)
            output.unlink()
            temporary = output.with_name(output.name + '.fixed.tmp')
            os.link(source, temporary)
            with patch('ncs_mcp.builder_release.uuid.uuid4') as random:
                random.return_value.hex = 'fixed'
                with self.assertRaises(BuilderAuthorizationError):
                    _write(output, {'overwrite': True}, builder_context=context)
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse(output.exists())

    def test_deploy_junction_escape_is_blocked_before_remote_calls(self):
        package = self.build()
        stage = Path(package['stage_dir'])
        external = self.root / 'relocated-stage'
        os.replace(stage, external)
        if os.name == 'nt':
            completed = subprocess.run(['cmd', '/c', 'mklink', '/J', str(stage), str(external)],
                                       capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        else:
            stage.symlink_to(external, target_is_directory=True)
        self.addCleanup(lambda: os.rmdir(stage) if os.name == 'nt' else stage.unlink())
        before = (self.version / 'release.json').read_bytes()
        count = len(self.commands)
        with self.assertRaises(BuilderError) as rejected:
            deploy_release(self.version,
                production_mcp_url='https://selected-project.vercel.app/api/mcp',
                runner=self.run_command, verifier=lambda *_: {'ok': True})
        self.assertIsInstance(rejected.exception.__cause__, BuilderAuthorizationError)
        self.assertEqual(len(self.commands), count)
        self.assertEqual((self.version / 'release.json').read_bytes(), before)

    def test_progress_revocation_stops_without_checkpoint_or_remote_mutation(self):
        self.build()
        before = (self.version / 'release.json').read_bytes()
        count = len(self.commands)
        def revoke(_):
            (self.root / '.state/ncs-data-builder/operation.lock').unlink()
        with self.assertRaises(BuilderError) as rejected:
            deploy_release(self.version,
                production_mcp_url='https://selected-project.vercel.app/api/mcp',
                runner=self.run_command, progress=revoke)
        self.assertIsInstance(rejected.exception.__cause__, BuilderAuthorizationError)
        self.assertEqual(len(self.commands), count)
        self.assertEqual((self.version / 'release.json').read_bytes(), before)

    def test_same_contents_directory_swap_during_progress_is_rejected(self):
        package = self.build()
        stage = Path(package['stage_dir'])
        before = (self.version / 'release.json').read_bytes()
        count = len(self.commands)
        def swap(_):
            replacement = self.version / 'release/original-stage'
            stage.rename(replacement)
            shutil.copytree(replacement, stage)
        with self.assertRaises(BuilderError) as rejected:
            deploy_release(self.version,
                production_mcp_url='https://selected-project.vercel.app/api/mcp',
                runner=self.run_command, progress=swap)
        self.assertIsInstance(rejected.exception.__cause__, BuilderAuthorizationError)
        self.assertEqual((self.version / 'release.json').read_bytes(), before)
        self.assertEqual(len(self.commands), count)

    def test_writer_lost_lease_before_replace_preserves_report_and_pending_temp(self):
        output = self.version / 'release.json'
        output.write_text('{"known_good":true}')
        before = output.read_bytes()
        real_fsync = os.fsync
        with DataBuilder(self.root).exclusive('package', self.version.name) as context:
            def revoke(fd):
                real_fsync(fd)
                (self.root / '.state/ncs-data-builder/operation.lock').unlink()
            with patch('ncs_mcp.builder_release.os.fsync', side_effect=revoke):
                with self.assertRaises(BuilderAuthorizationError):
                    _write(output, {'changed': True}, builder_context=context)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(len(list(self.version.glob('release.json.*.tmp'))), 1)

    def test_auth_loss_after_promotion_preserves_pending_intent_without_rollback(self):
        self.build()
        def revoke_after_promote(argv, cwd):
            result = self.run_command(argv, cwd)
            if argv[1] == 'promote':
                (self.root / '.state/ncs-data-builder/operation.lock').unlink()
            return result
        with self.assertRaises(BuilderError) as rejected:
            deploy_release(self.version,
                production_mcp_url='https://selected-project.vercel.app/api/mcp',
                runner=revoke_after_promote, verifier=lambda *_: {'ok': True})
        self.assertIsInstance(rejected.exception.__cause__, BuilderAuthorizationError)
        report = json.loads((self.version / 'release.json').read_text())
        self.assertEqual(report['deployment_transaction']['state'], 'promotion_pending')
        self.assertEqual(self.commands[-1][0][1], 'promote')
        self.assertFalse(any(argv[1] == 'rollback' for argv, _ in self.commands))

    def test_verifier_authorization_error_is_never_converted_to_rollback(self):
        self.build()
        def verify(url, _):
            if url == 'https://selected-project.vercel.app/api/mcp':
                raise BuilderAuthorizationError('lease revoked')
            return {'ok': True}
        with self.assertRaises(BuilderError) as rejected:
            deploy_release(self.version,
                production_mcp_url='https://selected-project.vercel.app/api/mcp',
                runner=self.run_command, verifier=verify)
        self.assertIsInstance(rejected.exception.__cause__, BuilderAuthorizationError)
        report = json.loads((self.version / 'release.json').read_text())
        self.assertEqual(report['deployment_transaction']['state'], 'production_verification_pending')
        self.assertFalse(any(argv[1] == 'rollback' for argv, _ in self.commands))

    def test_data_builder_deployed_pointer_requires_lease_after_release_returns(self):
        self.build()
        engine = DataBuilder(self.root)
        def revoked_success(*args, **kwargs):
            self.assertIsNotNone(kwargs.get('builder_context'))
            (engine.state / 'operation.lock').unlink()
            return {'ok': True}
        with patch.object(engine, 'candidate', return_value=self.version / 'ncs.db'), \
                patch('ncs_mcp.builder_release.deploy_release', side_effect=revoked_success):
            with self.assertRaises(BuilderError) as rejected:
                engine.deploy(self.version.name, self.template,
                              'https://selected-project.vercel.app/api/mcp')
        self.assertIsInstance(rejected.exception.__cause__, BuilderAuthorizationError)
        self.assertFalse((engine.state / 'deployed.json').exists())

    def test_isolated_package_and_explicit_project(self):
        timestamps = iter(
            (
                datetime(2026, 9, 12, 1, 2, 2, tzinfo=timezone.utc),
                datetime(2026, 9, 12, 1, 2, 5, tzinfo=timezone.utc),
            )
        )
        report = build_release(
            self.version,
            repo_root=self.root,
            deploy_root=self.template,
            expected_source_sha256=self.sha,
            runner=self.run_command,
            clock=lambda: next(timestamps),
        )
        self.assertTrue(report['ok'], report)
        stage = Path(report['stage_dir'])
        self.assertFalse((stage / 'ncs.db').exists())
        self.assertFalse((stage / '.env').exists())
        self.assertFalse((stage / '.state').exists())
        self.assertEqual(project_configuration(stage)['projectName'], 'selected-project')
        self.assertEqual(json.loads((stage / 'vercel.json').read_text())['env']['NCS_MCP_BUILD_ID'],
                         report['build_id'])
        self.assertEqual(report['started_at'], '2026-09-12T01:02:02+00:00')
        self.assertEqual(report['finished_at'], '2026-09-12T01:02:05+00:00')
        self.assertEqual(
            report['source']['sha256'], 'sha256:' + report['source_sha256']
        )
        self.assertEqual(
            report['lineage']['snapshot_build_generated_at'],
            '2026-09-12T01:02:03+00:00',
        )
        for artifact in report['lineage']['artifacts'].values():
            self.assertRegex(artifact['sha256'], r'^sha256:[0-9a-f]{64}$')
            self.assertGreater(artifact['bytes'], 0)
        self.assertTrue(report['function_bundle_verification']['exact_paths_verified'])
        self.assertEqual(
            Path(report['function_bundle_verification']['function_bundle_path']),
            stage / '.vercel/output/functions/python.func',
        )
        self.assertRegex(report['prebuilt_output']['sha256'], r'^sha256:[0-9a-f]{64}$')
        self.assertGreater(report['prebuilt_output']['file_count'], 0)
        self.assertIn(
            ['build', '--prod', '--yes'],
            [argv[1:] for argv, _ in self.commands],
        )

    def test_template_containment_canonicalizes_both_root_and_child(self):
        original_resolve = Path.resolve
        lexical_template = self.template
        canonical_root = self.root / 'canonical-long-name/deploy/vercel_mcp_app'
        lexical_child = lexical_template / 'api/index.py'

        def resolve_with_alias(path, *args, **kwargs):
            if path == lexical_template:
                return canonical_root
            try:
                relative = path.relative_to(lexical_template)
            except ValueError:
                return original_resolve(path, *args, **kwargs)
            return canonical_root / relative

        with patch.object(Path, 'resolve', resolve_with_alias):
            contained = _is_canonically_within(lexical_child, lexical_template)

        self.assertTrue(contained)

    def test_source_change_blocks_build(self):
        (self.version / 'ncs.db').write_bytes(b'changed')
        self.assertEqual(self.build()['status'], 'build_failed')
        self.assertEqual(self.commands, [])

    def test_untracked_required_local_module_fails_before_snapshot_or_vercel_build(self):
        core = self.template / 'src/ncs_mcp/search/core.py'
        core.parent.mkdir(parents=True)
        core.write_text('from .normalization import normalize_search_text\n')
        normalization = core.parent / 'normalization.py'
        normalization.write_text('def normalize_search_text(value): return value\n')
        self.files.append('src/ncs_mcp/search/core.py')
        report = self.build()
        self.assertEqual(report['status'], 'build_failed')
        self.assertIn('ncs_mcp.search.normalization', report['error'])
        self.assertIn('version control', report['error'])
        self.assertFalse((self.version / 'release/deploy/src/ncs_mcp/search/normalization.py').exists())
        self.assertFalse(any('--source' in argv for argv, _ in self.commands))
        self.assertFalse(any(len(argv) > 1 and argv[1] == 'build' for argv, _ in self.commands))

    def test_untracked_module_in_from_dot_import_fails_before_build(self):
        package = self.template / 'src/ncs_mcp/search'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('', encoding='utf-8')
        (package / 'core.py').write_text('from . import normalization\n', encoding='utf-8')
        (package / 'normalization.py').write_text('VALUE = 1\n', encoding='utf-8')
        self.files.extend([
            'src/ncs_mcp/search/__init__.py',
            'src/ncs_mcp/search/core.py',
        ])
        report = self.build()
        self.assertEqual(report['status'], 'build_failed')
        self.assertIn('ncs_mcp.search.normalization', report['error'])
        self.assertFalse(any('--source' in argv for argv, _ in self.commands))

    def test_candidate_counts_replace_only_source_floors_without_source_writes(self):
        config = {'env': {'NCS_MCP_READINESS_MIN_ROWS': json.dumps({
            'ksa_items': 574279, 'ksa_atomic_items': 644384,
            'ontology_concepts': 533909, 'ksa_concept_links': 574279,
            'ksa_atomic_concept_links': 644384, 'ncs_qualification_items': 1,
            'training_transition_gold_scenarios': 100,
            'training_transition_scenario_reviews': 11,
            'ontology_concept_label_candidates': 755})}}
        (self.template / 'vercel.json').write_text(json.dumps(config))
        before = (self.version / 'ncs.db').read_bytes()
        report = self.build()
        self.assertTrue(report['ok'], report)
        audit = report['readiness_floors']
        self.assertEqual(audit['previous']['ksa_items'], 574279)
        self.assertEqual(audit['resulting']['ksa_items'], 1)
        for table in ('ncs_qualification_items', 'training_transition_gold_scenarios',
                      'training_transition_scenario_reviews', 'ontology_concept_label_candidates'):
            self.assertEqual(audit['previous'][table], audit['resulting'][table])
        self.assertEqual(before, (self.version / 'ncs.db').read_bytes())
        self.assertTrue(audit['source_read_only'])

    def test_empty_candidate_source_table_blocks_package(self):
        source = self.version / 'ncs.db'
        with sqlite3.connect(source) as connection:
            connection.execute('DELETE FROM ksa_atomic_items')
        connection.close()
        self.sha = hashlib.sha256(source.read_bytes()).hexdigest()
        self.assertEqual(self.build()['status'], 'build_failed')

    def test_rebuild_preserves_existing_release(self):
        self.build()
        before = (self.version / 'release.json').read_bytes()
        with self.assertRaises(ReleaseError):
            self.build()
        self.assertEqual(before, (self.version / 'release.json').read_bytes())

    def test_wrong_project_url_blocks_deployment(self):
        self.build()
        count = len(self.commands)
        report = deploy_release(self.version, production_mcp_url='https://wrong.vercel.app/api/mcp',
                                runner=self.run_command)
        self.assertFalse(report['ok'])
        self.assertEqual(len(self.commands), count)

    def test_transport_pass_with_wrong_build_identity_is_failure(self):
        destination = self.root / 'verification.json'
        def run_verify(argv, cwd):
            destination.write_text(json.dumps({'ok': True, 'checks': {
                'initialize': {'server_version': '1.0+git.wrong-build'}}}))
        result = _verify('https://test.vercel.app/api/mcp', 'expected-build', self.root,
                         run_verify, destination)
        self.assertFalse(result['ok'])

    def test_default_deployment_verifier_checks_health_ready_and_mcp_identity(self):
        destination = self.root / 'verification.json'

        def request(url):
            if url.endswith('/api/health'):
                return 200, {'status': 'ok'}
            if url.endswith('/api/ready'):
                return 200, {'status': 'ready'}
            self.fail(url)

        def run_verify(argv, cwd):
            destination.write_text(json.dumps({'ok': True, 'checks': {
                'initialize': {'server_version': '1.0+git.expected-build'}}}))

        result = _verify_deployment(
            'https://selected-project-new.vercel.app/api/mcp',
            'expected-build', self.root, run_verify, destination, requester=request,
        )
        self.assertTrue(result['ok'])
        self.assertTrue(result['health']['ok'])
        self.assertTrue(result['ready']['ok'])
        self.assertTrue(result['mcp']['build_identity_matches'])

    def test_success_requires_two_verifications(self):
        self.build()
        checks = []
        def verify(url, build_id):
            checks.append((url, build_id))
            return {'ok': True, 'build_identity_matches': True}
        report = deploy_release(self.version,
                                production_mcp_url='https://selected-project.vercel.app/api/mcp',
                                runner=self.run_command, verifier=verify)
        self.assertEqual(report['status'], 'deployed')
        self.assertEqual(len(checks), 2)
        self.assertFalse(report['baseline_advanced'])
        deploy = [argv for argv, _ in self.commands if 'deploy' in argv]
        self.assertIn('--skip-domain', deploy[0])
        self.assertIn('--prebuilt', deploy[0])
        self.assertEqual(
            deploy[0][1:],
            ['deploy', '--prebuilt', '--prod', '--skip-domain', '--yes'],
        )
        self.assertEqual(report['previous_production']['deployment_url'],
                         self.previous_deployment)
        self.assertEqual(report['production_reconfirmation'], report['previous_production'])
        self.assertEqual(report['production_after_promotion']['deployment_url'],
                         self.staged_deployment)
        self.assertTrue(all(cwd == self.version / 'release/deploy'
                            for argv, cwd in self.commands if 'promote' in argv or 'deploy' in argv))

    def test_failed_staging_does_not_promote(self):
        self.build()
        result = deploy_release(self.version, production_mcp_url='https://selected-project.vercel.app/api/mcp',
                                runner=self.run_command, verifier=lambda *_: {'ok': False})
        self.assertFalse(result['ok'])
        self.assertEqual(result['failed_phase'], 'staged')
        self.assertFalse(any('promote' in argv for argv, _ in self.commands))

    def test_production_change_before_promotion_blocks_promotion(self):
        self.build()
        inspect_count = 0

        def runner(argv, cwd):
            nonlocal inspect_count
            if len(argv) > 1 and argv[1] == 'inspect':
                inspect_count += 1
                if inspect_count == 2:
                    return ('id dpl_external\n'
                            'url https://selected-project-external.vercel.app\n'
                            'name selected-project')
            return self.run_command(argv, cwd)

        result = deploy_release(
            self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=runner,
            verifier=lambda *_: {'ok': True},
        )
        self.assertFalse(result['ok'])
        self.assertEqual(result['failed_phase'], 'reconfirm_previous_production')
        self.assertFalse(any('promote' in argv for argv, _ in self.commands))

    def test_failed_post_promotion_verification_rolls_back_exact_previous_deployment(self):
        self.build()
        verifications = iter(({'ok': True}, {'ok': False}))
        result = deploy_release(
            self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=self.run_command,
            verifier=lambda *_: next(verifications),
        )
        self.assertFalse(result['ok'])
        self.assertEqual(result['failed_phase'], 'production')
        self.assertTrue(result['rollback_attempted'])
        self.assertTrue(result['rollback_performed'])
        self.assertTrue(result['rollback_reconfirmed'])
        rollback = [argv for argv, _ in self.commands if 'rollback' in argv]
        self.assertEqual(rollback[-1][2], self.previous_deployment)
        self.assertIn('--non-interactive', rollback[-1])
        self.assertEqual(self.current_deployment, self.previous_deployment)

    def test_ambiguous_promote_error_reconfirms_and_rolls_back_if_staged_is_live(self):
        self.build()

        def runner(argv, cwd):
            if len(argv) > 1 and argv[1] == 'promote':
                self.current_deployment = argv[2]
                raise RuntimeError('lost acknowledgement after remote promotion')
            return self.run_command(argv, cwd)

        result = deploy_release(
            self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=runner,
            verifier=lambda *_: {'ok': True},
        )
        self.assertFalse(result['ok'])
        self.assertTrue(result['promotion_outcome_reconfirmation_attempted'])
        self.assertEqual(
            result['promotion_outcome_reconfirmation']['deployment_url'],
            self.staged_deployment,
        )
        self.assertTrue(result['promotion_performed'])
        self.assertTrue(result['rollback_reconfirmed'])
        self.assertEqual(self.current_deployment, self.previous_deployment)

    def test_process_interruption_after_promotion_resumes_by_restoring_known_good(self):
        self.build()

        def interrupted_runner(argv, cwd):
            if len(argv) > 1 and argv[1] == 'promote':
                self.current_deployment = argv[2]
                raise KeyboardInterrupt()
            return self.run_command(argv, cwd)

        with self.assertRaises(KeyboardInterrupt):
            deploy_release(
                self.version,
                production_mcp_url='https://selected-project.vercel.app/api/mcp',
                runner=interrupted_runner,
                verifier=lambda *_: {'ok': True},
            )
        interrupted = json.loads((self.version / 'release.json').read_text())
        self.assertEqual(interrupted['status'], 'deploying')
        self.assertEqual(interrupted['deployment_transaction']['state'], 'promotion_pending')
        self.assertEqual(
            interrupted['deployment_transaction']['known_good_production']['deployment_url'],
            self.previous_deployment,
        )

        result = deploy_release(
            self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=self.run_command,
            verifier=lambda *_: {'ok': True},
        )
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['rollback_reconfirmed'])
        self.assertEqual(
            result['deployment_transaction']['known_good_production']['deployment_url'],
            self.previous_deployment,
        )

    def test_unconfirmed_rollback_blocks_retry_and_preserves_original_known_good(self):
        self.build()

        def rollback_failure(argv, cwd):
            if len(argv) > 1 and argv[1] == 'rollback':
                raise RuntimeError('rollback unavailable')
            return self.run_command(argv, cwd)

        verifications = iter(({'ok': True}, {'ok': False}))
        failed = deploy_release(
            self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=rollback_failure,
            verifier=lambda *_: next(verifications),
        )
        self.assertEqual(failed['deployment_transaction']['state'], 'rollback_unconfirmed')
        self.assertEqual(
            failed['deployment_transaction']['known_good_production']['deployment_url'],
            self.previous_deployment,
        )

        calls = []
        retry = deploy_release(
            self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=lambda argv, cwd: calls.append((argv, cwd)),
            verifier=lambda *_: self.fail('verification must not run'),
        )
        self.assertFalse(retry['ok'])
        self.assertEqual(retry['failed_phase'], 'preflight')
        self.assertIn('automatic retry is blocked', retry['error'])
        self.assertEqual(calls, [])
        self.assertEqual(
            retry['deployment_transaction']['known_good_production']['deployment_url'],
            self.previous_deployment,
        )

    def test_external_production_after_promote_blocks_rollback_and_preserves_known_good(self):
        self.build()
        external = 'https://selected-project-external.vercel.app'
        self.deployment_ids[external] = 'dpl_external'

        def runner(argv, cwd):
            result = self.run_command(argv, cwd)
            if len(argv) > 1 and argv[1] == 'promote':
                self.current_deployment = external
            return result

        report = deploy_release(
            self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=runner,
            verifier=lambda *_: {'ok': True},
        )
        self.assertFalse(report['ok'])
        self.assertEqual(report['deployment_transaction']['state'], 'production_diverged')
        self.assertFalse(report['rollback_attempted'])
        self.assertFalse(any('rollback' in argv for argv, _ in self.commands))
        self.assertEqual(
            report['deployment_transaction']['known_good_production']['deployment_url'],
            self.previous_deployment,
        )

    def test_external_production_during_verification_blocks_rollback(self):
        self.build()
        external = 'https://selected-project-external.vercel.app'
        self.deployment_ids[external] = 'dpl_external'
        verification_count = 0

        def verify(*_):
            nonlocal verification_count
            verification_count += 1
            if verification_count == 2:
                self.current_deployment = external
                return {'ok': False}
            return {'ok': True}

        report = deploy_release(
            self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=self.run_command,
            verifier=verify,
        )
        self.assertFalse(report['ok'])
        self.assertEqual(report['deployment_transaction']['state'], 'production_diverged')
        self.assertFalse(report['rollback_attempted'])
        self.assertEqual(
            report['rollback_precondition']['expected_current']['deployment_id'],
            'dpl_staged',
        )
        self.assertEqual(
            report['rollback_precondition']['observed_current']['deployment_id'],
            'dpl_external',
        )
        self.assertFalse(any('rollback' in argv for argv, _ in self.commands))
        self.assertEqual(
            report['deployment_transaction']['known_good_production']['deployment_url'],
            self.previous_deployment,
        )

    def test_already_restored_known_good_skips_rollback_command(self):
        self.build()
        verification_count = 0

        def verify(*_):
            nonlocal verification_count
            verification_count += 1
            if verification_count == 2:
                self.current_deployment = self.previous_deployment
                return {'ok': False}
            return {'ok': True}

        report = deploy_release(
            self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=self.run_command,
            verifier=verify,
        )
        self.assertFalse(report['ok'])
        self.assertEqual(report['deployment_transaction']['state'], 'rollback_confirmed')
        self.assertTrue(report['deployment_transaction']['rollback_already_restored'])
        self.assertFalse(report['rollback_attempted'])
        self.assertTrue(report['rollback_reconfirmed'])
        self.assertFalse(any('rollback' in argv for argv, _ in self.commands))

    def test_external_production_before_interrupted_resume_blocks_rollback(self):
        self.build()

        def interrupted_runner(argv, cwd):
            if len(argv) > 1 and argv[1] == 'promote':
                self.current_deployment = argv[2]
                raise KeyboardInterrupt()
            return self.run_command(argv, cwd)

        with self.assertRaises(KeyboardInterrupt):
            deploy_release(
                self.version,
                production_mcp_url='https://selected-project.vercel.app/api/mcp',
                runner=interrupted_runner,
                verifier=lambda *_: {'ok': True},
            )
        external = 'https://selected-project-external.vercel.app'
        self.deployment_ids[external] = 'dpl_external'
        self.current_deployment = external
        rollback_count = sum('rollback' in argv for argv, _ in self.commands)
        deploy_count = sum('deploy' in argv for argv, _ in self.commands)
        report = deploy_release(
            self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=self.run_command,
            verifier=lambda *_: self.fail('verification must not run'),
        )
        self.assertFalse(report['ok'])
        self.assertEqual(report['deployment_transaction']['state'], 'production_diverged')
        self.assertEqual(sum('rollback' in argv for argv, _ in self.commands), rollback_count)
        self.assertEqual(sum('deploy' in argv for argv, _ in self.commands), deploy_count)
        self.assertEqual(
            report['deployment_transaction']['known_good_production']['deployment_url'],
            self.previous_deployment,
        )

    def test_legacy_promoted_failure_migrates_to_durable_block_across_retries(self):
        self.build()
        path = self.version / 'release.json'
        legacy = json.loads(path.read_text())
        legacy.update(
            status='deploy_failed', ok=False, promotion_performed=True,
            rollback_attempted=True, rollback_reconfirmed=False,
            previous_production={
                'deployment_id': 'dpl_previous',
                'deployment_url': self.previous_deployment,
            },
        )
        legacy.pop('deployment_transaction', None)
        path.write_text(json.dumps(legacy), encoding='utf-8')

        first_calls = []
        first = deploy_release(
            self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=lambda argv, cwd: first_calls.append((argv, cwd)),
        )
        self.assertFalse(first['ok'])
        self.assertIn('Legacy deployment evidence', first['error'])
        self.assertEqual(first_calls, [])
        self.assertEqual(
            first['deployment_transaction']['state'],
            'promotion_outcome_unconfirmed',
        )
        self.assertEqual(
            first['deployment_transaction']['known_good_production']['deployment_url'],
            self.previous_deployment,
        )

        second_calls = []
        second = deploy_release(
            self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=lambda argv, cwd: second_calls.append((argv, cwd)),
        )
        self.assertFalse(second['ok'])
        self.assertIn('automatic retry is blocked', second['error'])
        self.assertEqual(second_calls, [])
        self.assertEqual(
            second['deployment_transaction']['known_good_production']['deployment_url'],
            self.previous_deployment,
        )

        # A fresh interpreter has only the durable release.json checkpoint.
        persisted = json.loads(path.read_text(encoding='utf-8'))
        path.write_text(json.dumps(persisted), encoding='utf-8')
        script = """
import json
import sys
from pathlib import Path
from ncs_mcp.builder_release import deploy_release
from ncs_mcp.data_builder import DataBuilder

calls = []
version = Path(sys.argv[1])
with DataBuilder(version.parent.parent.parent.parent).exclusive('deploy', version.name) as context:
    report = deploy_release(
        version,
        production_mcp_url='https://selected-project.vercel.app/api/mcp',
        runner=lambda argv, cwd: calls.append(argv), builder_context=context,
    )
print(json.dumps({'report': report, 'calls': calls}))
"""
        restarted_env = os.environ.copy()
        source_root = str((Path(__file__).resolve().parents[1] / 'src').resolve())
        inherited_pythonpath = restarted_env.get('PYTHONPATH')
        restarted_env['PYTHONPATH'] = (
            source_root
            if not inherited_pythonpath
            else source_root + os.pathsep + inherited_pythonpath
        )
        restarted_process = subprocess.run(
            [sys.executable, '-c', script, str(self.version)],
            check=False,
            capture_output=True,
            text=True,
            env=restarted_env,
        )
        self.assertEqual(restarted_process.returncode, 0, restarted_process.stderr)
        restarted_payload = json.loads(restarted_process.stdout)
        restarted = restarted_payload['report']
        self.assertFalse(restarted['ok'])
        self.assertIn('automatic retry is blocked', restarted['error'])
        self.assertEqual(restarted_payload['calls'], [])
        self.assertEqual(
            restarted['deployment_transaction']['known_good_production']['deployment_url'],
            self.previous_deployment,
        )

    def test_legacy_remote_risk_hidden_behind_blocked_attempt_is_migrated(self):
        self.build()
        path = self.version / 'release.json'
        legacy = json.loads(path.read_text())
        known_good = {
            'deployment_id': 'dpl_previous',
            'deployment_url': self.previous_deployment,
        }
        legacy.update(status='deploy_failed', ok=False)
        legacy.pop('deployment_transaction', None)
        legacy['deployment_attempts'] = [
            {
                'attempt': 1,
                'status': 'deploy_failed',
                'promotion_performed': True,
                'rollback_attempted': True,
                'rollback_reconfirmed': False,
                'previous_production': known_good,
            },
            {
                'attempt': 2,
                'status': 'deploy_failed',
                'failed_phase': 'preflight',
                'error': 'Legacy deployment evidence blocked the prior retry.',
            },
        ]
        for key in (
            'promotion_performed', 'rollback_attempted', 'rollback_reconfirmed',
            'previous_production',
        ):
            legacy.pop(key, None)
        path.write_text(json.dumps(legacy), encoding='utf-8')

        calls = []
        report = deploy_release(
            self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=lambda argv, cwd: calls.append((argv, cwd)),
        )
        self.assertFalse(report['ok'])
        self.assertIn('Legacy deployment evidence', report['error'])
        self.assertEqual(calls, [])
        self.assertEqual(
            report['deployment_transaction']['state'],
            'promotion_outcome_unconfirmed',
        )
        self.assertEqual(
            report['deployment_transaction']['known_good_production'],
            known_good,
        )

    def test_interrupted_promotion_inspection_failure_remains_fail_closed(self):
        self.build()

        def interrupted_runner(argv, cwd):
            if len(argv) > 1 and argv[1] == 'promote':
                self.current_deployment = argv[2]
                raise KeyboardInterrupt()
            return self.run_command(argv, cwd)

        with self.assertRaises(KeyboardInterrupt):
            deploy_release(
                self.version,
                production_mcp_url='https://selected-project.vercel.app/api/mcp',
                runner=interrupted_runner,
                verifier=lambda *_: {'ok': True},
            )

        def inspect_failure(argv, cwd):
            if len(argv) > 1 and argv[1] == 'inspect':
                raise RuntimeError('temporary inspection failure')
            self.fail(argv)

        report = deploy_release(
            self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=inspect_failure,
        )
        self.assertFalse(report['ok'])
        self.assertEqual(
            report['deployment_transaction']['state'],
            'promotion_outcome_unconfirmed',
        )
        calls = []
        retry = deploy_release(
            self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=lambda argv, cwd: calls.append((argv, cwd)),
        )
        self.assertFalse(retry['ok'])
        self.assertEqual(calls, [])

    def test_interrupted_promotion_preflight_failure_remains_fail_closed(self):
        self.build()

        def interrupted_runner(argv, cwd):
            if len(argv) > 1 and argv[1] == 'promote':
                self.current_deployment = argv[2]
                raise KeyboardInterrupt()
            return self.run_command(argv, cwd)

        with self.assertRaises(KeyboardInterrupt):
            deploy_release(
                self.version,
                production_mcp_url='https://selected-project.vercel.app/api/mcp',
                runner=interrupted_runner,
                verifier=lambda *_: {'ok': True},
            )
        calls = []
        with patch('ncs_mcp.builder_release._tree_evidence',
                   side_effect=OSError('temporary local read failure')):
            report = deploy_release(
                self.version,
                production_mcp_url='https://selected-project.vercel.app/api/mcp',
                runner=lambda argv, cwd: calls.append((argv, cwd)),
            )
        self.assertFalse(report['ok'])
        self.assertEqual(report['failed_phase'], 'preflight')
        self.assertEqual(
            report['deployment_transaction']['state'],
            'promotion_outcome_unconfirmed',
        )
        self.assertEqual(calls, [])

    def test_atomic_report_write_flushes_file_before_replace(self):
        destination = self.version / 'release.json'
        with DataBuilder(self.root).exclusive('package', self.version.name) as context:
            with patch('ncs_mcp.builder_release.os.fsync', wraps=os.fsync) as fsync:
                _write(destination, {'state': 'durable'}, builder_context=context)
        self.assertEqual(json.loads(destination.read_text()), {'state': 'durable'})
        self.assertGreaterEqual(fsync.call_count, 1)

    def test_added_source_or_changed_archive_blocks_upload(self):
        self.build()
        (self.version / 'release/deploy/raw.db').write_bytes(b'raw-source')
        count = len(self.commands)
        result = deploy_release(self.version, production_mcp_url='https://selected-project.vercel.app/api/mcp',
                                runner=self.run_command)
        self.assertEqual(result['failed_phase'], 'preflight')
        self.assertEqual(len(self.commands), count)

    def test_vercel_generated_metadata_does_not_invalidate_package(self):
        report = self.build()
        stage = Path(report['stage_dir'])
        generated = stage / '.vercel/python/.venv/Lib/site-packages/example'
        generated.mkdir(parents=True)
        (generated / '__init__.py').write_text('generated = True')
        (stage / '.vercel/.env.production.local').write_text('GENERATED=1')
        generated_build = stage / 'build/lib/ncs_mcp'
        generated_build.mkdir(parents=True)
        (generated_build / '__init__.py').write_text('generated = True')
        generated_metadata = stage / 'src/ncs_mcp.egg-info'
        generated_metadata.mkdir(parents=True)
        (generated_metadata / 'PKG-INFO').write_text('generated')
        project_path = stage / '.vercel/project.json'
        project = json.loads(project_path.read_text())
        project['settings'] = {'framework': 'python'}
        project_path.write_text(json.dumps(project))

        result = deploy_release(
            self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=self.run_command,
            verifier=self.verified,
        )
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['status'], 'deployed')

    def test_changed_prebuilt_output_blocks_upload_before_vercel_or_network(self):
        report = self.build()
        bundle = Path(report['function_bundle_verification']['function_bundle_path'])
        (bundle / 'handler.py').write_text('tampered = True')
        count = len(self.commands)
        result = deploy_release(
            self.version,
            production_mcp_url='https://selected-project.vercel.app/api/mcp',
            runner=self.run_command,
        )
        self.assertEqual(result['failed_phase'], 'preflight')
        self.assertIn('prebuilt output changed', result['error'])
        self.assertEqual(len(self.commands), count)

    def test_errors_do_not_leak_runner_credentials(self):
        self.build()
        def fail(*_):
            raise RuntimeError('secret-service-key')
        result = deploy_release(self.version, production_mcp_url='https://selected-project.vercel.app/api/mcp',
                                runner=fail)
        self.assertNotIn('secret-service-key', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
