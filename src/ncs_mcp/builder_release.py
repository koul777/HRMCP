"""Isolated compact snapshot packaging and verified Vercel promotion for Builder.

No operation updates the source DB or a preprocessing baseline. Callers decide
when to expose deploy_release as an explicit operator action.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import uuid
from typing import Callable


class ReleaseError(RuntimeError):
    """A bounded, credential-free release failure."""


SOURCE_COUNT_TABLES = ('ksa_items', 'ksa_atomic_items', 'ontology_concepts',
                       'ksa_concept_links', 'ksa_atomic_concept_links')


def _candidate_readiness_floors(source: Path, config: dict) -> dict:
    """Replace only source-dependent floors from a closed, read-only candidate."""
    env = config.setdefault('env', {})
    original = json.loads(env.get('NCS_MCP_READINESS_MIN_ROWS', '{}'))
    if not isinstance(original, dict):
        raise ReleaseError('Deployment readiness row floors are invalid.')
    updated = dict(original)
    connection = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    try:
        connection.execute('PRAGMA query_only=ON')
        for table in SOURCE_COUNT_TABLES:
            count = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            if count <= 0:
                raise ReleaseError('Candidate ontology source tables must contain evidence rows.')
            updated[table] = count
    finally:
        connection.close()
    env['NCS_MCP_READINESS_MIN_ROWS'] = json.dumps(updated, separators=(',', ':'))
    return {'previous': original, 'resulting': updated, 'source_read_only': True,
            'updated_tables': list(SOURCE_COUNT_TABLES)}


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _write(path: Path, value: dict) -> None:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def project_configuration(deploy_root: Path) -> dict:
    root = Path(deploy_root).resolve()
    project = json.loads((root / '.vercel/project.json').read_text(encoding='utf-8-sig'))
    result = {key: project.get(key) for key in ('projectId', 'orgId', 'projectName')}
    if not all(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]+', value)
               for value in result.values()):
        raise ReleaseError('Selected directory has no valid linked Vercel project.')
    result['deploy_root'] = str(root)
    result['production_mcp_url'] = f"https://{result['projectName']}.vercel.app/api/mcp"
    return result


def _run(argv: list[str], cwd: Path) -> str:
    # Never retain subprocess stderr (CLI errors can contain credentials).
    env = os.environ.copy()
    env.pop('VERCEL_PROJECT_ID', None)
    env.pop('VERCEL_ORG_ID', None)
    with tempfile.TemporaryFile() as output:
        completed = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.DEVNULL, shell=False)
        if completed.returncode:
            raise ReleaseError('Release command failed; check local CLI login and project access.')
        output.seek(max(0, output.tell() - 65536))
        return output.read().decode('utf-8', errors='replace')


def _run_with_progress(run: Callable, argv: list[str], cwd: Path,
                       path: Path, tell: Callable) -> str:
    """Relay atomic, bounded stage records; always stop the observer with the command."""
    stopped = threading.Event()
    previous = None

    def sample() -> None:
        nonlocal previous
        try:
            if path.stat().st_size > 4096:
                return
            record = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(record, dict) or not isinstance(record.get('stage'), str)
                    or type(record.get('completed')) is not int
                    or record['completed'] not in range(4)
                    or record.get('total') not in (None, 3)
                    or record.get('unit') != '공정 완료'):
                return
            if record != previous:
                tell(record)
                previous = record
        except (OSError, ValueError):
            pass

    def observe() -> None:
        while not stopped.wait(0.2):
            sample()

    observer = threading.Thread(target=observe, daemon=True)
    observer.start()
    try:
        return run(argv, cwd)
    finally:
        stopped.set()
        observer.join()
        sample()


def build_release(version_dir: Path, *, repo_root: Path, deploy_root: Path,
                  expected_source_sha256: str, progress: Callable | None = None,
                  runner: Callable | None = None) -> dict:
    """Build a new immutable package; refuses reuse of an existing release folder."""
    version = Path(version_dir).resolve()
    repo = Path(repo_root).resolve()
    run = runner or _run
    tell = progress or (lambda message: None)
    report = {'schema': 'ncs_builder_release_v1', 'ok': False, 'status': 'building',
              'source_database_mutated': False, 'baseline_advanced': False,
              'package_validated': False}
    output = version / 'release.json'
    if output.exists() or (version / 'release').exists():
        raise ReleaseError('This version already has release artifacts; create a new version to rebuild.')
    try:
        source = version / 'ncs.db'
        tell({'stage': '경량 DB 원본 무결성 검사 중', 'completed': 0, 'total': None, 'unit': '공정 완료'})
        expected = expected_source_sha256.removeprefix('sha256:').lower()
        if not re.fullmatch('[0-9a-f]{64}', expected) or _hash(source) != expected:
            raise ReleaseError('Prepared source hash changed; build the version again.')
        if any(Path(str(source) + suffix).exists() for suffix in ('-wal', '-journal')):
            raise ReleaseError('Prepared source has an active SQLite sidecar.')
        report['project'] = project_configuration(deploy_root)
        report['source_sha256'] = expected
        report['repo_root'] = str(repo)
        report['build_id'] = uuid.uuid4().hex
        release = version / 'release'
        release.mkdir(exist_ok=False)
        stage = release / 'deploy'
        stage.mkdir()
        template = repo / 'deploy/vercel_mcp_app'
        files = run(['git', 'ls-files', '-z', '--', 'deploy/vercel_mcp_app'], repo).split('\0')
        copied = []
        for name in files:
            if not name:
                continue
            original = repo / name
            relative = original.relative_to(template)
            if (original.is_symlink() or not original.resolve().is_relative_to(template)
                    or any(part.startswith('.env') or part in {'.state', '.vercel'} for part in relative.parts)):
                raise ReleaseError('Deployment template contains an unsafe file.')
            allowed = (relative.suffix == '.py' or relative.as_posix() in {
                'requirements.txt', 'pyproject.toml', 'uv.lock', 'vercel.json',
                '.vercelignore', '.gitignore', '.python-version'})
            if not allowed:
                continue
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
            copied.append(relative.as_posix())
        if not {'api/index.py', 'vercel.json', 'requirements.txt'}.issubset(copied):
            raise ReleaseError('Tracked deployment template is incomplete.')
        (stage / '.vercel').mkdir()
        _write(stage / '.vercel/project.json', {
            key: report['project'][key] for key in ('projectId', 'orgId', 'projectName')})
        config_path = stage / 'vercel.json'
        config = json.loads(config_path.read_text(encoding='utf-8'))
        config.setdefault('env', {})['NCS_MCP_BUILD_ID'] = report['build_id']
        report['readiness_floors'] = _candidate_readiness_floors(source, config)
        _write(config_path, config)
        tell({'stage': '경량 DB 생성 준비 중', 'completed': 0, 'total': 3, 'unit': '공정 완료'})
        archive = stage / 'api/ncs_ontology_compact.zip'
        manifest = stage / 'api/ncs_ontology_compact.manifest.json'
        build_report = release / 'snapshot-build.json'
        try:
            _run_with_progress(run, [sys.executable, str(repo / 'scripts/build_vercel_snapshot.py'),
                 '--source', str(source), '--output-db', str(release / 'compact.db'),
                 '--archive', str(archive), '--manifest', str(manifest),
                 '--report', str(build_report), '--progress-file', str(release / 'progress.json')],
                 repo, release / 'progress.json', tell)
        except ReleaseError as exc:
            if build_report.exists():
                details = json.loads(build_report.read_text(encoding='utf-8'))
                report['failed_build_stage'] = (details.get('error') or {}).get('stage')
                raise ReleaseError('Compact build failed; see release/snapshot-build.json for the failed stage.') from exc
            raise
        evidence = json.loads(build_report.read_text(encoding='utf-8'))
        if evidence.get('ok') is not True:
            raise ReleaseError('Compact snapshot verification did not pass.')
        tell({'stage': '배포 패키지 무결성 검사 중', 'completed': 0, 'total': None, 'unit': '공정 완료'})
        if _hash(source) != expected:
            raise ReleaseError('Prepared source changed during packaging.')
        report['stage_dir'] = str(stage)
        report['artifacts'] = {p.relative_to(stage).as_posix(): _hash(p)
                               for p in stage.rglob('*') if p.is_file()}
        if not archive.is_file() or not manifest.is_file():
            raise ReleaseError('Verified compact package is missing.')
        report.update(ok=True, status='package_ready', package_validated=True)
        tell({'stage': '경량 DB·배포 패키지 준비 완료', 'completed': 3, 'total': 3, 'unit': '공정 완료'})
    except Exception as exc:
        report.update(status='build_failed', error=str(exc) if isinstance(exc, ReleaseError)
                      else 'Package preparation failed; inspect input paths and local prerequisites.')
    _write(output, report)
    return report


def _verify(url: str, build_id: str, repo: Path, run: Callable, destination: Path) -> dict:
    run([sys.executable, str(repo / 'scripts/verify_remote_mcp_transport.py'), url,
         '--concurrency', '2', '--out', str(destination)], repo)
    result = json.loads(destination.read_text(encoding='utf-8'))
    version = result.get('checks', {}).get('initialize', {}).get('server_version', '')
    return {'ok': result.get('ok') is True and version.endswith('+git.' + build_id),
            'server_version': version, 'build_identity_matches': version.endswith('+git.' + build_id)}


def deploy_release(version_dir: Path, *, production_mcp_url: str,
                   progress: Callable | None = None, runner: Callable | None = None,
                   verifier: Callable | None = None) -> dict:
    """Stage, verify exact identity, promote, then verify the production alias."""
    version = Path(version_dir).resolve()
    output = version / 'release.json'
    report = json.loads(output.read_text(encoding='utf-8'))
    run = runner or _run
    tell = progress or (lambda message: None)
    phase = 'preflight'
    attempt_fields = ('ok', 'status', 'failed_phase', 'error', 'staged_url',
                      'staged_verification', 'promotion_performed',
                      'production_verification', 'production_mcp_url')
    history = report.setdefault('deployment_attempts', [])
    # Preserve legacy failure evidence before clearing fields for a fresh attempt.
    if report.get('status') == 'deploy_failed' and not history:
        history.append({key: report[key] for key in attempt_fields if key in report})
    previous_status = report.get('status')
    previous_ok = report.get('ok')
    for key in attempt_fields:
        if key not in ('ok', 'status'):
            report.pop(key, None)
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        tell({'stage': '배포 전 무결성 검사 중', 'completed': 0, 'total': None, 'unit': '공정 완료'})
        if not ((previous_status == 'package_ready' and previous_ok is True)
                or (previous_status == 'deploy_failed' and report.get('package_validated') is True)):
            raise ReleaseError('Build and validate a fresh package before deployment.')
        if not re.fullmatch(r'https://[a-zA-Z0-9.-]+/api/mcp', production_mcp_url):
            raise ReleaseError('Production MCP URL must be an HTTPS /api/mcp endpoint.')
        if production_mcp_url != report['project']['production_mcp_url']:
            raise ReleaseError('Production URL does not match the explicitly selected Vercel project.')
        stage = Path(report['stage_dir']).resolve()
        if stage != version / 'release/deploy':
            raise ReleaseError('Deployment directory is outside this version package.')
        if any(Path(str(version / 'ncs.db') + suffix).exists() for suffix in ('-wal', '-journal')):
            raise ReleaseError('Prepared source has an active SQLite sidecar.')
        if _hash(version / 'ncs.db') != report['source_sha256']:
            raise ReleaseError('Prepared source changed after packaging.')
        actual = {p.relative_to(stage).as_posix(): _hash(p) for p in stage.rglob('*') if p.is_file()}
        if actual != report['artifacts'] or any(p.is_symlink() for p in stage.rglob('*')):
            raise ReleaseError('Deployment package changed after verification.')
        repo = Path(report['repo_root'])
        verify = verifier or (lambda url, build_id: _verify(
            url, build_id, repo, run, version / 'release' / (phase + '-verification.json')))
        phase = 'staging'
        tell({'stage': 'Vercel 검증용 배포 중', 'completed': 0, 'total': 4, 'unit': '공정 완료'})
        executable = shutil.which('vercel') or 'vercel'
        stdout = run([executable, 'deploy', '--prod', '--skip-domain', '--yes'], stage)
        urls = re.findall(r'https://[A-Za-z0-9-]+\.vercel\.app', stdout)
        if not urls:
            raise ReleaseError('Vercel did not return a staging deployment URL.')
        staged = urls[-1]
        report['staged_url'] = staged
        phase = 'staged'
        tell({'stage': '검증용 MCP 연결 확인 중', 'completed': 1, 'total': 4, 'unit': '공정 완료'})
        evidence = verify(staged + '/api/mcp', report['build_id'])
        report['staged_verification'] = evidence
        if not evidence.get('ok'):
            raise ReleaseError('Staged MCP transport or build identity verification failed.')
        phase = 'promotion'
        tell({'stage': '운영 주소에 배포 연결 중', 'completed': 2, 'total': 4, 'unit': '공정 완료'})
        run([executable, 'promote', staged, '--yes'], stage)
        report['promotion_performed'] = True
        phase = 'production'
        tell({'stage': '운영 MCP 연결 확인 중', 'completed': 3, 'total': 4, 'unit': '공정 완료'})
        evidence = verify(production_mcp_url, report['build_id'])
        report['production_verification'] = evidence
        if not evidence.get('ok'):
            raise ReleaseError('Production verification failed; baseline remains unchanged.')
        report.update(ok=True, status='deployed', production_mcp_url=production_mcp_url)
        tell({'stage': 'Vercel MCP 업데이트 완료', 'completed': 4, 'total': 4, 'unit': '공정 완료'})
    except Exception as exc:
        report.update(ok=False, status='deploy_failed', failed_phase=phase,
                      error=str(exc) if isinstance(exc, ReleaseError)
                      else 'Release operation failed; baseline remains unchanged.')
    history.append({'attempt': len(history) + 1, 'started_at': started_at,
                    'finished_at': datetime.now(timezone.utc).isoformat(),
                    **{key: report[key] for key in attempt_fields if key in report}})
    _write(output, report)
    return report
