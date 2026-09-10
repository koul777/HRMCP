import hashlib
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ncs_mcp.builder_release import (
    ReleaseError, _is_canonically_within, _verify, build_release, deploy_release,
    project_configuration,
)


class BuilderReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.version = self.root / '.state/versions/v1'
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

    def run_command(self, argv, cwd):
        self.commands.append((argv, cwd))
        if argv[0] == 'git':
            return '\0'.join('deploy/vercel_mcp_app/' + name for name in self.files) + '\0'
        if '--source' in argv:
            for flag in ('--output-db', '--archive', '--manifest'):
                Path(argv[argv.index(flag) + 1]).write_bytes(b'compact')
            Path(argv[argv.index('--report') + 1]).write_text('{"ok":true}')
            return ''
        return 'https://staged-build.vercel.app'

    def build(self):
        return build_release(self.version, repo_root=self.root, deploy_root=self.template,
                             expected_source_sha256=self.sha, runner=self.run_command)

    def test_isolated_package_and_explicit_project(self):
        report = self.build()
        self.assertTrue(report['ok'], report)
        stage = Path(report['stage_dir'])
        self.assertFalse((stage / 'ncs.db').exists())
        self.assertFalse((stage / '.env').exists())
        self.assertFalse((stage / '.state').exists())
        self.assertEqual(project_configuration(stage)['projectName'], 'selected-project')
        self.assertEqual(json.loads((stage / 'vercel.json').read_text())['env']['NCS_MCP_BUILD_ID'],
                         report['build_id'])

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
        self.assertTrue(all(cwd == self.version / 'release/deploy'
                            for argv, cwd in self.commands if 'promote' in argv or 'deploy' in argv))

    def test_failed_staging_does_not_promote(self):
        self.build()
        result = deploy_release(self.version, production_mcp_url='https://selected-project.vercel.app/api/mcp',
                                runner=self.run_command, verifier=lambda *_: {'ok': False})
        self.assertFalse(result['ok'])
        self.assertEqual(result['failed_phase'], 'staged')
        self.assertFalse(any('promote' in argv for argv, _ in self.commands))

    def test_added_source_or_changed_archive_blocks_upload(self):
        self.build()
        (self.version / 'release/deploy/raw.db').write_bytes(b'raw-source')
        count = len(self.commands)
        result = deploy_release(self.version, production_mcp_url='https://selected-project.vercel.app/api/mcp',
                                runner=self.run_command)
        self.assertEqual(result['failed_phase'], 'preflight')
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
