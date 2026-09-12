"""Isolated compact snapshot packaging and verified Vercel promotion for Builder.

No operation updates the source DB or a preprocessing baseline. Mutation entry
points require the exact live package/deploy capability issued by DataBuilder.
"""
from __future__ import annotations

import ast
import hashlib
from functools import partial
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from typing import Callable

from .builder_authorization import (
    BuilderAuthorizationError, BuilderOperationContext, require_builder_context,
)


class ReleaseGuard:
    """A live lease plus stable, non-redirecting release directory identities."""

    def __init__(self, context: BuilderOperationContext | None, *, action: str,
                 version_dir: Path, repo_root: Path | None = None):
        require_builder_context(context, action=action, root=repo_root,
                                version_dir=version_dir)
        self.context = context
        self.action = action
        self.version = _absolute_path(version_dir)
        self.root = Path(context.root)
        self.state = Path(context.state_dir)
        self._directories: dict[str, tuple[int, int]] = {}
        self.check()

    def check(self, *paths: Path) -> None:
        require_builder_context(self.context, action=self.action, root=self.root,
                                state_dir=self.state, version=self.version.name,
                                version_dir=self.version)
        # Check these ancestors even for a report-only failure path.
        for path in (self.version, self.version / 'release',
                     self.version / 'release/deploy',
                     self.version / 'release/deploy/.vercel/output', *paths):
            self._path(path)

    def _path(self, value: Path) -> None:
        path = _absolute_path(value)
        lexical_root = self.version.parent.parent.parent.parent
        try:
            relative = path.relative_to(lexical_root)
        except ValueError:
            # Canonical root spelling is used for state pointers on Windows.
            try:
                relative = path.relative_to(self.root)
            except ValueError as exc:
                raise BuilderAuthorizationError('Release path escapes Builder root.') from exc
            lexical_root = self.root
        expected = self.root / relative
        if path.resolve() != expected:
            raise BuilderAuthorizationError('Release path redirects outside its authorized location.')
        current = lexical_root
        for part in ('', *relative.parts):
            if part:
                current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & 0x400):
                raise BuilderAuthorizationError('Release path contains a reparse point.')
            if stat.S_ISDIR(info.st_mode):
                key = os.path.normcase(str(current.resolve()))
                identity = (info.st_dev, info.st_ino)
                previous = self._directories.setdefault(key, identity)
                if previous != identity:
                    raise BuilderAuthorizationError('Release directory identity changed.')
            elif info.st_nlink != 1 or not stat.S_ISREG(info.st_mode):
                raise BuilderAuthorizationError('Release file is linked or not a regular file.')

    def output(self, path: Path) -> None:
        self.check(path)
        resolved = path.resolve()
        release = self.version.resolve() / 'release'
        if not (resolved.is_relative_to(release)
                or resolved == self.version.resolve() / 'release.json'
                or (self.action == 'deploy' and resolved == self.state / 'deployed.json')):
            raise BuilderAuthorizationError('Output is outside the Builder release artifacts.')
        source = self.version / 'ncs.db'
        if resolved == source.resolve() or (path.exists() and source.exists()
                                            and os.path.samefile(path, source)):
            raise BuilderAuthorizationError('Release output aliases its source database.')

    def call(self, function: Callable, *args, **kwargs):
        self.check()
        try:
            return function(*args, **kwargs)
        finally:
            self.check()

    def command(self, runner: Callable, argv: list[str], cwd: Path) -> str:
        self.check(cwd)
        for flag in ('--out', '--report', '--progress-file'):
            if flag in argv:
                self.output(Path(argv[argv.index(flag) + 1]))
        return self.call(runner, argv, cwd)


def package_guard(builder_context: BuilderOperationContext | None) -> ReleaseGuard:
    context = require_builder_context(builder_context, action='package')
    if not context.version:
        raise BuilderAuthorizationError('A version-bound package operation is required.')
    return ReleaseGuard(context, action='package',
                        version_dir=Path(context.state_dir) / 'versions' / context.version)


class ReleaseError(RuntimeError):
    """A bounded, credential-free release failure."""


def _absolute_path(path: str | Path) -> Path:
    """Make *path* absolute without expanding Windows 8.3 path spelling."""

    return Path(os.path.abspath(Path(path).expanduser()))


def _is_canonically_within(path: Path, root: Path) -> bool:
    """Check containment after normalizing both sides of a path alias."""

    return path.resolve(strict=True).is_relative_to(root.resolve(strict=True))


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


def _artifact_evidence(path: Path, *, digest: str | None = None) -> dict:
    """Return explicit lineage evidence without changing legacy raw hashes."""
    return {'path': str(path), 'bytes': path.stat().st_size,
            'sha256': 'sha256:' + (digest if digest is not None else _hash(path))}


def _stage_artifacts(stage: Path) -> dict[str, str]:
    """Hash immutable staged inputs, excluding only generated Build Output API files."""
    artifacts: dict[str, str] = {}
    for path in stage.rglob('*'):
        relative = path.relative_to(stage)
        if relative.parts[:2] == ('.vercel', 'output'):
            continue
        info = path.lstat()
        if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ReleaseError('Deployment package contains a symlink.')
        if path.is_file():
            artifacts[relative.as_posix()] = _hash(path)
    return artifacts


def _tree_evidence(root: Path) -> dict:
    """Return a bounded identity for one exact generated directory tree."""
    if not root.is_dir():
        raise ReleaseError('Fresh Vercel production build output is missing.')
    digest = hashlib.sha256()
    total_bytes = 0
    file_count = 0
    for path in sorted(root.rglob('*'), key=lambda candidate: candidate.as_posix()):
        info = path.lstat()
        if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ReleaseError('Fresh Vercel production build contains a symlink.')
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        file_hash = _hash(path)
        digest.update(relative.encode('utf-8'))
        digest.update(b'\0')
        digest.update(str(size).encode('ascii'))
        digest.update(b'\0')
        digest.update(file_hash.encode('ascii'))
        digest.update(b'\n')
        total_bytes += size
        file_count += 1
    if file_count == 0:
        raise ReleaseError('Fresh Vercel production build output is empty.')
    return {'path': str(root), 'bytes': total_bytes, 'file_count': file_count,
            'sha256': 'sha256:' + digest.hexdigest()}


def _module_source_relative(module: str) -> tuple[Path, Path] | None:
    if module == 'api' or module.startswith('api.'):
        base = Path(*module.split('.'))
    elif module == 'ncs_mcp' or module.startswith('ncs_mcp.'):
        base = Path('src', *module.split('.'))
    else:
        return None
    return base.with_suffix('.py'), base / '__init__.py'


def _validate_isolated_source_package(_template: Path, stage: Path) -> None:
    """Reject local imports whose repository source exists but was not tracked/copied."""
    missing: set[str] = set()
    for source in sorted(stage.rglob('*.py')):
        relative = source.relative_to(stage)
        if relative.parts[:2] == ('.vercel', 'output'):
            continue
        if relative.parts[0] == 'src':
            module_parts = relative.with_suffix('').parts[1:]
        else:
            module_parts = relative.with_suffix('').parts
        is_package = module_parts[-1] == '__init__'
        module = '.'.join(module_parts[:-1] if is_package else module_parts)
        package = module if is_package else module.rpartition('.')[0]
        try:
            parsed = ast.parse(source.read_text(encoding='utf-8'), filename=str(relative))
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise ReleaseError('Tracked deployment source package cannot be parsed.') from exc
        imports: set[str] = set()
        for node in ast.walk(parsed):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    try:
                        target = importlib.util.resolve_name(
                            '.' * node.level + (node.module or ''), package
                        )
                    except (ImportError, ValueError) as exc:
                        raise ReleaseError('Tracked deployment source has an invalid relative import.') from exc
                else:
                    target = node.module or ''
                if target:
                    imports.add(target)
                    # ``from . import normalization`` names the containing
                    # package in ``target`` and the required module only in
                    # the imported aliases. Treat an alias as a module only
                    # when that module exists in the source template; this
                    # catches omitted local files without misclassifying
                    # ordinary imported functions as modules.
                    for alias in node.names:
                        if alias.name == '*':
                            continue
                        child = f'{target}.{alias.name}'
                        child_candidates = _module_source_relative(child)
                        if child_candidates is None:
                            continue
                        if (any((_template / candidate).is_file()
                                for candidate in child_candidates)
                                and not any((stage / candidate).is_file()
                                            for candidate in child_candidates)):
                            missing.add(child)
        for imported in imports:
            candidates = _module_source_relative(imported)
            if candidates is None:
                continue
            stage_candidates = tuple(stage / candidate for candidate in candidates)
            if not any(candidate.is_file() for candidate in stage_candidates):
                missing.add(imported)
    if missing:
        modules = ', '.join(sorted(missing))
        raise ReleaseError(
            'Tracked deployment source package is incomplete; required imported modules '
            f'were not copied into the isolated stage: {modules}. Add the source files to '
            'version control before rebuilding.'
        )


def _utc_timestamp(clock: Callable[[], datetime] | None = None) -> str:
    value = clock() if clock is not None else datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReleaseError('Clock must return a timezone-aware datetime.')
    return value.astimezone(timezone.utc).isoformat()


def _write(path: Path, value: dict, *, builder_context: BuilderOperationContext,
           guard: ReleaseGuard | None = None) -> None:
    require_builder_context(builder_context, action=('package', 'deploy'))
    if not builder_context.version:
        raise BuilderAuthorizationError('Release writes require a version-bound operation.')
    guard = guard or ReleaseGuard(builder_context, action=('deploy' if path.name == 'deployed.json'
                                  else builder_context.action),
                                  version_dir=Path(builder_context.state_dir) / 'versions' / builder_context.version)
    if type(guard) is not ReleaseGuard or guard.context is not builder_context:
        raise BuilderAuthorizationError('Release writer guard does not match its capability.')
    guard.output(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    identity = None
    try:
        guard.check(temporary)
        with temporary.open('x', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            identity = (info.st_dev, info.st_ino)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        guard.output(path)
        guard.check(temporary)
        info = temporary.lstat()
        if (info.st_dev, info.st_ino) != identity:
            raise BuilderAuthorizationError('Release temporary file identity changed.')
        os.replace(temporary, path)
        guard.check(path)
        # POSIX needs a directory fsync to make the rename durable. Windows
        # does not support opening directories this way on every filesystem,
        # so retain the atomic replace and treat directory sync as best effort.
        try:
            flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
            directory = os.open(path.parent, flags)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
    finally:
        # A lost lease must leave prior durable intent intact and cannot clean
        # up a replacement owner's paths. Never replace the authorization error.
        guard.check()
        if identity is not None and temporary.exists():
            guard.check(temporary)
            info = temporary.lstat()
            if (info.st_dev, info.st_ino) != identity:
                raise BuilderAuthorizationError('Release temporary file identity changed.')
            temporary.unlink()


def _copy_template_file(source: Path, target: Path, *, guard: ReleaseGuard) -> None:
    """Copy through an exclusively created file; never open the public target for writing."""
    guard.check(source)
    guard.output(target)
    expected = _hash(source)
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.copytmp')
    identity = None
    try:
        guard.output(temporary)
        with source.open('rb') as incoming, temporary.open('xb') as outgoing:
            info = os.fstat(outgoing.fileno())
            identity = (info.st_dev, info.st_ino)
            digest = hashlib.sha256()
            for chunk in iter(lambda: incoming.read(1024 * 1024), b''):
                guard.check(source, temporary)
                outgoing.write(chunk)
                digest.update(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        guard.check(source, temporary)
        if (digest.hexdigest() != expected or _hash(source) != expected
                or _hash(temporary) != expected):
            raise ReleaseError('Tracked deployment source changed during package copy.')
        guard.output(target)
        info = temporary.lstat()
        if (info.st_dev, info.st_ino) != identity:
            raise BuilderAuthorizationError('Deployment copy temporary identity changed.')
        os.replace(temporary, target)
        guard.check(target)
        if _hash(target) != expected:
            raise ReleaseError('Deployment source copy changed after atomic replacement.')
    finally:
        guard.check()
        if identity is not None and temporary.exists():
            guard.output(temporary)
            info = temporary.lstat()
            if (info.st_dev, info.st_ino) != identity:
                raise BuilderAuthorizationError('Deployment copy temporary identity changed.')
            temporary.unlink()


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
                  runner: Callable | None = None,
                  clock: Callable[[], datetime] | None = None,
                  builder_context: BuilderOperationContext | None = None) -> dict:
    """Build a new immutable package; refuses reuse of an existing release folder."""
    version = _absolute_path(version_dir)
    repo = _absolute_path(repo_root)
    guard = ReleaseGuard(builder_context, action='package', version_dir=version, repo_root=repo)
    run = partial(guard.command, runner or _run)
    tell = partial(guard.call, progress or (lambda message: None))
    write = partial(_write, builder_context=builder_context, guard=guard)
    report = {'schema': 'ncs_builder_release_v1', 'started_at': _utc_timestamp(clock),
              'ok': False, 'status': 'building',
              'package_operation_lineage': builder_context.lineage(),
              'source_database_mutated': False, 'baseline_advanced': False,
              'package_validated': False}
    output = version / 'release.json'
    if output.exists() or (version / 'release').exists():
        raise ReleaseError('This version already has release artifacts; create a new version to rebuild.')
    try:
        source = version / 'ncs.db'
        tell({'stage': '경량 DB 원본 무결성 검사 중', 'completed': 0, 'total': None, 'unit': '공정 완료'})
        expected = expected_source_sha256.removeprefix('sha256:').lower()
        if not re.fullmatch('[0-9a-f]{64}', expected):
            raise ReleaseError('Prepared source hash is not a valid SHA-256 digest.')
        actual_source_sha256 = _hash(source)
        report['source'] = _artifact_evidence(source, digest=actual_source_sha256)
        report['expected_source_sha256'] = 'sha256:' + expected
        if actual_source_sha256 != expected:
            raise ReleaseError('Prepared source hash changed; build the version again.')
        if any(Path(str(source) + suffix).exists() for suffix in ('-wal', '-journal')):
            raise ReleaseError('Prepared source has an active SQLite sidecar.')
        report['project'] = project_configuration(deploy_root)
        report['source_sha256'] = expected
        report['repo_root'] = str(repo)
        report['build_id'] = uuid.uuid4().hex
        release = version / 'release'
        guard.output(release)
        release.mkdir(exist_ok=False)
        stage = release / 'deploy'
        guard.output(stage)
        stage.mkdir()
        template = repo / 'deploy/vercel_mcp_app'
        files = run(['git', 'ls-files', '-z', '--', 'deploy/vercel_mcp_app'], repo).split('\0')
        copied = []
        for name in files:
            if not name:
                continue
            original = repo / name
            relative = original.relative_to(template)
            # Compare canonical paths on both sides.  GitHub Windows runners
            # can spell ``repo`` as RUNNER~1 while resolve() expands an
            # individual file to runneradmin; comparing that resolved child to
            # the lexical template falsely rejects every tracked file.
            if (original.is_symlink()
                    or not _is_canonically_within(original, template)
                    or any(part.startswith('.env') or part in {'.state', '.vercel'} for part in relative.parts)):
                raise ReleaseError('Deployment template contains an unsafe file.')
            allowed = (relative.suffix == '.py' or relative.as_posix() in {
                'requirements.txt', 'pyproject.toml', 'uv.lock', 'vercel.json',
                '.vercelignore', '.gitignore', '.python-version'})
            if not allowed:
                continue
            target = stage / relative
            guard.output(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            guard.output(target)
            _copy_template_file(original, target, guard=guard)
            guard.check(target)
            copied.append(relative.as_posix())
        if not {'api/index.py', 'vercel.json', 'requirements.txt'}.issubset(copied):
            raise ReleaseError('Tracked deployment template is incomplete.')
        _validate_isolated_source_package(template, stage)
        guard.output(stage / '.vercel')
        (stage / '.vercel').mkdir()
        write(stage / '.vercel/project.json', {
            key: report['project'][key] for key in ('projectId', 'orgId', 'projectName')})
        config_path = stage / 'vercel.json'
        config = json.loads(config_path.read_text(encoding='utf-8'))
        config.setdefault('env', {})['NCS_MCP_BUILD_ID'] = report['build_id']
        report['readiness_floors'] = _candidate_readiness_floors(source, config)
        write(config_path, config)
        tell({'stage': '경량 DB 생성 준비 중', 'completed': 0, 'total': 3, 'unit': '공정 완료'})
        archive = stage / 'api/ncs_ontology_compact.zip'
        manifest = stage / 'api/ncs_ontology_compact.manifest.json'
        build_report = release / 'snapshot-build.json'
        try:
            from scripts.build_vercel_snapshot import build_snapshot

            evidence = guard.call(
                build_snapshot, source=source, output_db=release / 'compact.db',
                archive=archive, manifest=manifest, report_path=build_report,
                progress_file=release / 'progress.json', progress_callback=tell,
                builder_context=builder_context,
            )
            write(build_report, evidence)
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
        if not archive.is_file() or not manifest.is_file():
            raise ReleaseError('Verified compact package is missing.')
        report['stage_dir'] = str(stage)
        report['artifacts'] = _stage_artifacts(stage)
        executable = shutil.which('vercel') or 'vercel'
        tell({'stage': 'Vercel 운영 빌드 생성 중', 'completed': 3, 'total': 5, 'unit': '공정 완료'})
        run([executable, 'build', '--prod', '--yes'], stage)
        build_output = stage / '.vercel/output'
        function_bundle = build_output / 'functions/python.func'
        if not function_bundle.is_dir():
            raise ReleaseError(
                'Fresh Vercel production build did not create the required '
                '.vercel/output/functions/python.func bundle.'
            )
        tell({'stage': 'Vercel 함수 번들 검증 중', 'completed': 4, 'total': 5, 'unit': '공정 완료'})
        bundle_report = release / 'function-bundle-verification.json'
        try:
            run([
                sys.executable,
                str(repo / 'scripts/verify_vercel_compact_package.py'),
                '--archive', str(archive),
                '--manifest', str(manifest),
                '--function-bundle', str(function_bundle),
                '--out', str(bundle_report),
            ], repo)
        except ReleaseError as exc:
            raise ReleaseError(
                'Fresh Vercel function bundle verification failed; see '
                'release/function-bundle-verification.json.'
            ) from exc
        try:
            bundle_evidence = json.loads(bundle_report.read_text(encoding='utf-8'))
            measured = bundle_evidence['function_bundle']
            exact_paths = (
                Path(bundle_evidence['archive_path']).resolve() == archive.resolve()
                and Path(bundle_evidence['manifest_path']).resolve() == manifest.resolve()
                and Path(measured['path']).resolve() == function_bundle.resolve()
            )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReleaseError('Fresh Vercel function bundle verifier evidence is invalid.') from exc
        if not (bundle_evidence.get('ok') is True
                and measured.get('checked') is True
                and measured.get('required') is True
                and measured.get('ok') is True
                and exact_paths):
            raise ReleaseError('Fresh Vercel function bundle verification did not pass exactly.')
        report['prebuilt_output'] = _tree_evidence(build_output)
        report['function_bundle_verification'] = {
            'path': str(bundle_report),
            'sha256': 'sha256:' + _hash(bundle_report),
            'function_bundle_path': str(function_bundle),
            'function_bundle_bytes': measured.get('bytes'),
            'exact_paths_verified': True,
        }
        report['lineage'] = {
            'snapshot_build_generated_at': evidence.get('generated_at'),
            'source': report['source'],
            'artifacts': {
                'compact_database': _artifact_evidence(release / 'compact.db'),
                'archive': _artifact_evidence(
                    archive, digest=report['artifacts']['api/ncs_ontology_compact.zip']
                ),
                'manifest': _artifact_evidence(
                    manifest,
                    digest=report['artifacts']['api/ncs_ontology_compact.manifest.json'],
                ),
                'snapshot_build_report': _artifact_evidence(build_report),
                'function_bundle_verification': _artifact_evidence(bundle_report),
            },
        }
        report.update(ok=True, status='package_ready', package_validated=True)
        tell({'stage': '경량 DB·검증된 Vercel 빌드 준비 완료', 'completed': 5, 'total': 5, 'unit': '공정 완료'})
    except BuilderAuthorizationError:
        raise
    except Exception as exc:
        report.update(status='build_failed', error=str(exc) if isinstance(exc, ReleaseError)
                      else 'Package preparation failed; inspect input paths and local prerequisites.')
    report['finished_at'] = _utc_timestamp(clock)
    write(output, report)
    return report


def _verify(url: str, build_id: str, repo: Path, run: Callable, destination: Path) -> dict:
    run([sys.executable, str(repo / 'scripts/verify_remote_mcp_transport.py'), url,
         '--concurrency', '2', '--out', str(destination)], repo)
    result = json.loads(destination.read_text(encoding='utf-8'))
    version = result.get('checks', {}).get('initialize', {}).get('server_version', '')
    return {'ok': result.get('ok') is True and version.endswith('+git.' + build_id),
            'server_version': version, 'build_identity_matches': version.endswith('+git.' + build_id)}


def _request_json(url: str) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        headers={'Accept': 'application/json', 'User-Agent': 'ncs-builder-release/1'},
        method='GET',
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            status = int(response.status)
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return int(exc.code), {}
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
        return 0, {}
    return status, payload if isinstance(payload, dict) else {}


def _verify_deployment(mcp_url: str, build_id: str, repo: Path, run: Callable,
                       destination: Path,
                       requester: Callable[[str], tuple[int, dict]] | None = None) -> dict:
    """Verify health/readiness plus MCP behavior and exact immutable build identity."""
    base_url = mcp_url.removesuffix('/api/mcp')
    request_json = requester or _request_json
    surface_checks: dict[str, dict] = {}
    for name, expected_status in (('health', 'ok'), ('ready', 'ready')):
        status, payload = request_json(f'{base_url}/api/{name}')
        observed = payload.get('status') if isinstance(payload, dict) else None
        surface_checks[name] = {
            'ok': status == 200 and observed == expected_status,
            'http_status': status,
            'service_status': observed if isinstance(observed, str) else None,
        }
    mcp = _verify(mcp_url, build_id, repo, run, destination)
    return {
        'ok': all(check['ok'] for check in surface_checks.values()) and mcp['ok'],
        **surface_checks,
        'mcp': mcp,
        'build_identity_matches': mcp['build_identity_matches'],
    }


def _production_root(mcp_url: str) -> str:
    return mcp_url.removesuffix('/api/mcp')


def _parse_inspection(stdout: str, production_root: str) -> dict:
    """Extract only bounded deployment identity fields from Vercel inspect output."""
    deployment_ids = list(dict.fromkeys(re.findall(r'\bdpl_[A-Za-z0-9]+\b', stdout)))
    urls = list(dict.fromkeys(
        match.rstrip('/') for match in re.findall(
            r'https://[A-Za-z0-9-]+\.vercel\.app/?', stdout
        )
    ))
    unique_urls = [url for url in urls if url != production_root.rstrip('/')]
    if len(deployment_ids) > 1 or len(unique_urls) != 1:
        raise ReleaseError('Unable to identify exactly one current production deployment.')
    return {
        'deployment_id': deployment_ids[0] if deployment_ids else None,
        'deployment_url': unique_urls[0],
    }


def _inspect_production(executable: str, production_mcp_url: str,
                        stage: Path, run: Callable) -> dict:
    root = _production_root(production_mcp_url)
    stdout = run([executable, 'inspect', root, '--wait', '--no-color'], stage)
    return _parse_inspection(stdout, root)


def _same_deployment(first: dict, second: dict) -> bool:
    if first.get('deployment_id') and second.get('deployment_id'):
        return (first['deployment_id'] == second['deployment_id']
                and first.get('deployment_url') == second.get('deployment_url'))
    return first.get('deployment_url') == second.get('deployment_url')


_INTERRUPTED_PROMOTION_STATES = {
    'promotion_pending',
    'promotion_acknowledged',
    'production_verification_pending',
}
_UNCONFIRMED_REMOTE_STATES = {
    'rollback_pending',
    'rollback_unconfirmed',
    'promotion_outcome_unconfirmed',
    'production_diverged',
}


def _checkpoint(report: dict, output: Path, transaction: dict,
                state: str, *, builder_context: BuilderOperationContext,
                guard: ReleaseGuard, **fields) -> None:
    """Atomically persist remote-mutation intent before returning to the CLI."""
    transaction.update(fields)
    transaction['state'] = state
    transaction['updated_at'] = datetime.now(timezone.utc).isoformat()
    report['deployment_transaction'] = transaction
    report.update(ok=False, status='deploying')
    _write(output, report, builder_context=builder_context, guard=guard)


def _rollback_known_good(*, report: dict, output: Path, transaction: dict,
                         executable: str, production_mcp_url: str, stage: Path,
                         run: Callable, known_good: dict,
                         expected_current: dict, builder_context: BuilderOperationContext,
                         guard: ReleaseGuard) -> bool:
    """Restore known-good only when the exact expected staged target is live."""
    guard.check()
    _deployment_checkpoint = partial(_checkpoint, builder_context=builder_context, guard=guard)
    transaction.pop('rollback_already_restored', None)
    report['rollback_attempted'] = False
    expected_id = expected_current.get('deployment_id')
    expected_url = expected_current.get('deployment_url')
    if not expected_id or expected_url != transaction.get('staged_url'):
        report['rollback_performed'] = False
        report['rollback_reconfirmed'] = False
        _deployment_checkpoint(
            report, output, transaction, 'production_diverged',
            rollback_expected_current=expected_current,
        )
        return False
    try:
        current = _inspect_production(executable, production_mcp_url, stage, run)
        report['rollback_precondition'] = {
            'expected_current': expected_current,
            'observed_current': current,
        }
    except BuilderAuthorizationError:
        raise
    except Exception:
        report['rollback_performed'] = False
        report['rollback_reconfirmed'] = False
        _deployment_checkpoint(
            report, output, transaction, 'rollback_unconfirmed',
            rollback_expected_current=expected_current,
        )
        return False
    if _same_deployment(known_good, current):
        report['rollback_performed'] = False
        report['rollback_reconfirmed'] = True
        _deployment_checkpoint(
            report, output, transaction, 'rollback_confirmed',
            rollback_expected_current=expected_current,
            rollback_already_restored=True,
        )
        return True
    if not _same_deployment(expected_current, current):
        report['rollback_performed'] = False
        report['rollback_reconfirmed'] = False
        _deployment_checkpoint(
            report, output, transaction, 'production_diverged',
            rollback_expected_current=expected_current,
            observed_production=current,
        )
        return False
    report['rollback_attempted'] = True
    _deployment_checkpoint(
        report, output, transaction, 'rollback_pending',
        rollback_target=known_good,
        rollback_expected_current=expected_current,
    )
    try:
        run([
            executable, 'rollback', known_good['deployment_url'],
            '--non-interactive',
        ], stage)
        report['rollback_performed'] = True
    except BuilderAuthorizationError:
        raise
    except Exception:
        report['rollback_performed'] = False
        report['rollback_reconfirmed'] = False
        _deployment_checkpoint(report, output, transaction, 'rollback_unconfirmed')
        return False
    try:
        restored = _inspect_production(executable, production_mcp_url, stage, run)
        report['rollback_reconfirmation'] = restored
        report['rollback_reconfirmed'] = _same_deployment(known_good, restored)
    except BuilderAuthorizationError:
        raise
    except Exception:
        report['rollback_reconfirmed'] = False
    state = 'rollback_confirmed' if report.get('rollback_reconfirmed') else 'rollback_unconfirmed'
    _deployment_checkpoint(report, output, transaction, state)
    return state == 'rollback_confirmed'


def _legacy_remote_risk_evidence(report: dict, history: list) -> dict | None:
    """Find legacy remote-mutation evidence without trusting only the last retry."""
    candidates = [report]
    candidates.extend(
        attempt for attempt in reversed(history) if isinstance(attempt, dict)
    )
    for evidence in candidates:
        if (evidence.get('promotion_performed') is True
                or (
                    evidence.get('rollback_attempted') is True
                    and evidence.get('rollback_reconfirmed') is not True
                )):
            return evidence
    return None


def _legacy_known_good(evidence: dict, report: dict, history: list) -> dict | None:
    """Recover the original known-good identity for a conservative migration."""
    candidates = [evidence, report]
    candidates.extend(
        attempt for attempt in history if isinstance(attempt, dict)
    )
    for candidate in candidates:
        known_good = candidate.get('previous_production')
        if (isinstance(known_good, dict)
                and isinstance(known_good.get('deployment_url'), str)
                and known_good['deployment_url']):
            return dict(known_good)
    return None


def _release_package_preflight(report: dict, version: Path, production_mcp_url: str,
                               guard: ReleaseGuard) -> tuple[Path, Path]:
    """Validate the same immutable inputs for a first deployment and local recovery."""
    guard.check()
    if not re.fullmatch(r'https://[a-zA-Z0-9.-]+/api/mcp', production_mcp_url):
        raise ReleaseError('Production MCP URL must be an HTTPS /api/mcp endpoint.')
    if production_mcp_url != report['project']['production_mcp_url']:
        raise ReleaseError('Production URL does not match the explicitly selected Vercel project.')
    stage = version / 'release/deploy'
    if _absolute_path(report['stage_dir']).resolve() != stage.resolve():
        raise ReleaseError('Deployment directory is outside this version package.')
    if any(Path(str(version / 'ncs.db') + suffix).exists() for suffix in ('-wal', '-journal')):
        raise ReleaseError('Prepared source has an active SQLite sidecar.')
    if _hash(version / 'ncs.db') != report['source_sha256']:
        raise ReleaseError('Prepared source changed after packaging.')
    if _stage_artifacts(stage) != report['artifacts']:
        raise ReleaseError('Deployment package changed after verification.')
    selected_project = project_configuration(stage)
    for key in ('projectId', 'orgId', 'projectName', 'production_mcp_url'):
        if selected_project[key] != report['project'][key]:
            raise ReleaseError('Isolated stage no longer matches the explicitly selected Vercel project.')
    prebuilt_output = stage / '.vercel/output'
    if _tree_evidence(prebuilt_output) != report.get('prebuilt_output'):
        raise ReleaseError('Verified Vercel prebuilt output changed after bundle verification.')
    bundle_report = version / 'release/function-bundle-verification.json'
    recorded_bundle_report = report.get('function_bundle_verification', {})
    if (not bundle_report.is_file()
            or recorded_bundle_report.get('sha256') != 'sha256:' + _hash(bundle_report)
            or Path(recorded_bundle_report.get('function_bundle_path', '')).resolve()
                != (prebuilt_output / 'functions/python.func').resolve()
            or recorded_bundle_report.get('exact_paths_verified') is not True):
        raise ReleaseError('Exact Vercel function bundle verification evidence changed or is missing.')
    repo = Path(report['repo_root'])
    require_builder_context(guard.context, action='deploy', root=repo, version_dir=version)
    guard.check()
    return stage, repo


def _reconcile_deployed_release(report: dict, *, version: Path, production_mcp_url: str,
                                guard: ReleaseGuard, run: Callable, tell: Callable,
                                verifier: Callable | None) -> dict:
    """Revalidate a durable success without uploading, promoting, or rolling back.

    A failed recovery leaves release.json's last confirmed deployment intact so
    a temporary health failure cannot destroy the evidence needed by a retry.
    """
    recovered = json.loads(json.dumps(report))
    try:
        tell({'stage': '완료된 운영 배포와 로컬 계보 재확인 중', 'completed': 0,
              'total': 2, 'unit': '공정 완료'})
        stage, repo = _release_package_preflight(report, version, production_mcp_url, guard)
        transaction = report.get('deployment_transaction') or {}
        expected = report.get('production_after_promotion') or {}
        staged = transaction.get('staged_url')
        if not (
            report.get('ok') is True and report.get('status') == 'deployed'
            and report.get('package_validated') is True
            and transaction.get('schema') == 'ncs_builder_deployment_transaction_v1'
            and transaction.get('state') == 'deployed'
            and re.fullmatch(r'dpl_[A-Za-z0-9]+', str(expected.get('deployment_id', '')))
            and re.fullmatch(r'https://[A-Za-z0-9-]+\.vercel\.app', str(staged))
            and expected.get('deployment_url') == staged == report.get('staged_url')
            and _same_deployment(expected, transaction.get('observed_production') or {})
            and expected['deployment_id'] == (transaction.get('observed_production') or {}).get('deployment_id')
            and report.get('production_mcp_url') == production_mcp_url
            and (report.get('production_verification') or {}).get('ok') is True
            and re.fullmatch(r'[0-9a-f]{32}', str(report.get('build_id', '')))
        ):
            raise ReleaseError('Durable completed deployment identity is missing or inconsistent.')
        executable = shutil.which('vercel') or 'vercel'
        current = _inspect_production(executable, production_mcp_url, stage, run)
        if not (_same_deployment(expected, current)
                and current.get('deployment_id') == expected['deployment_id']):
            raise ReleaseError('Production diverged from the durably completed deployment; local recovery is blocked.')
        verify = partial(guard.call, verifier or (lambda url, build_id: _verify_deployment(
            url, build_id, repo, run, version / 'release' /
            ('reconciliation-' + guard.context.operation_id + '-verification.json'))))
        evidence = verify(production_mcp_url, report['build_id'])
        mcp = evidence.get('mcp') or {}
        if not (
            evidence.get('ok') is True
            and evidence.get('build_identity_matches') is True
            and (evidence.get('health') or {}).get('ok') is True
            and (evidence.get('ready') or {}).get('ok') is True
            and mcp.get('ok') is True and mcp.get('build_identity_matches') is True
            and str(mcp.get('server_version', '')).endswith('+git.' + report['build_id'])
        ):
            raise ReleaseError('Completed deployment health, readiness, or exact build identity revalidation failed.')
        reconfirmed = _inspect_production(executable, production_mcp_url, stage, run)
        if not (_same_deployment(expected, reconfirmed)
                and reconfirmed.get('deployment_id') == expected['deployment_id']):
            raise ReleaseError('Production changed during local recovery; local pointer remains unchanged.')
        _release_package_preflight(report, version, production_mcp_url, guard)
        recovered['deployment_operation_lineage'] = guard.context.lineage()
        recovered['local_reconciliation'] = {
            'schema': 'ncs_builder_deployment_reconciliation_v1',
            'verified_at': _utc_timestamp(),
            'operation_lineage': guard.context.lineage(),
            'deployment_identity': reconfirmed,
            'build_id': report['build_id'],
            'source_sha256': report['source_sha256'],
            'verification': evidence,
            'remote_mutation_performed': False,
            'baseline_advanced': False,
        }
        tell({'stage': '운영 배포 재검증 완료·로컬 계보 복구 중', 'completed': 2,
              'total': 2, 'unit': '공정 완료'})
        _write(version / 'release.json', recovered, builder_context=guard.context, guard=guard)
        return recovered
    except BuilderAuthorizationError:
        raise
    except Exception as exc:
        guard.check()
        recovered.update(ok=False, status='reconciliation_failed',
                         error=str(exc) if isinstance(exc, ReleaseError)
                         else 'Completed deployment could not be revalidated; local pointer remains unchanged.')
        return recovered


def deploy_release(version_dir: Path, *, production_mcp_url: str,
                   progress: Callable | None = None, runner: Callable | None = None,
                   verifier: Callable | None = None,
                   builder_context: BuilderOperationContext | None = None) -> dict:
    """Upload one verified prebuilt output, stage it, promote it, and roll back on failure."""
    version = _absolute_path(version_dir)
    guard = ReleaseGuard(builder_context, action='deploy', version_dir=version)
    _deployment_checkpoint = partial(_checkpoint, builder_context=builder_context, guard=guard)
    write = partial(_write, builder_context=builder_context, guard=guard)
    output = version / 'release.json'
    guard.output(output)
    report = json.loads(output.read_text(encoding='utf-8'))
    run = partial(guard.command, runner or _run)
    tell = partial(guard.call, progress or (lambda message: None))
    if report.get('status') == 'deployed':
        return _reconcile_deployed_release(report, version=version,
            production_mcp_url=production_mcp_url, guard=guard, run=run,
            tell=tell, verifier=verifier)
    report['deployment_operation_lineage'] = builder_context.lineage()
    phase = 'preflight'
    attempt_fields = (
        'ok', 'status', 'failed_phase', 'error', 'previous_production',
        'staged_url', 'staged_verification', 'production_reconfirmation',
        'promotion_performed', 'production_after_promotion',
        'production_verification', 'production_mcp_url', 'rollback_attempted',
        'rollback_performed', 'rollback_reconfirmation', 'rollback_reconfirmed',
        'resume_reconciliation', 'promotion_outcome_reconfirmation',
        'promotion_outcome_reconfirmation_attempted',
        'rollback_precondition',
    )
    history = report.setdefault('deployment_attempts', [])
    legacy_evidence = None
    if (
        report.get('status') == 'deploy_failed'
        and not isinstance(report.get('deployment_transaction'), dict)
    ):
        legacy_evidence = _legacy_remote_risk_evidence(report, history)
    legacy_remote_risk = legacy_evidence is not None
    # Preserve legacy failure evidence before clearing fields for a fresh attempt.
    if report.get('status') == 'deploy_failed' and not history:
        history.append({key: report[key] for key in attempt_fields if key in report})
    previous_status = report.get('status')
    previous_ok = report.get('ok')
    started_at = datetime.now(timezone.utc).isoformat()
    transaction = report.get('deployment_transaction')
    if not isinstance(transaction, dict):
        transaction = None
    if legacy_remote_risk:
        known_good = _legacy_known_good(legacy_evidence, report, history)
        transaction = {
            'schema': 'ncs_builder_deployment_transaction_v1',
            'started_at': started_at,
            'state': 'promotion_outcome_unconfirmed',
            'updated_at': started_at,
            'legacy_migration': {
                'source_status': 'deploy_failed',
                'promotion_performed': legacy_evidence.get('promotion_performed') is True,
                'rollback_attempted': legacy_evidence.get('rollback_attempted') is True,
                'rollback_reconfirmed': legacy_evidence.get('rollback_reconfirmed') is True,
                'automatic_retry_blocked': True,
            },
        }
        if known_good is not None:
            transaction['known_good_production'] = known_good
        report['deployment_transaction'] = transaction
        # Make the fail-closed migration durable before legacy attempt fields are
        # cleared or any deployment validation can reach a remote CLI command.
        write(output, report)
    for key in attempt_fields:
        if key not in ('ok', 'status'):
            report.pop(key, None)
    executable = shutil.which('vercel') or 'vercel'
    stage: Path | None = None
    previous_production: dict | None = None
    staged: str | None = None
    try:
        tell({'stage': '배포 전 무결성 검사 중', 'completed': 0, 'total': None, 'unit': '공정 완료'})
        if not ((previous_status == 'package_ready' and previous_ok is True)
                or (previous_status in {'deploy_failed', 'deploying'}
                    and report.get('package_validated') is True)):
            raise ReleaseError('Build and validate a fresh package before deployment.')
        if legacy_remote_risk:
            raise ReleaseError(
                'Legacy deployment evidence records a promotion or unconfirmed rollback '
                'without a durable known-good transaction; automatic retry is blocked.'
            )
        stage, repo = _release_package_preflight(report, version, production_mcp_url, guard)
        prebuilt_output = stage / '.vercel/output'
        verify = partial(guard.call, verifier or (lambda url, build_id: _verify_deployment(
            url, build_id, repo, run,
            version / 'release' / (phase + '-verification.json'))))
        known_good = (transaction or {}).get('known_good_production')
        if transaction is not None and transaction.get('state') in _UNCONFIRMED_REMOTE_STATES:
            raise ReleaseError(
                'The previous deployment left production or rollback unconfirmed; '
                'automatic retry is blocked pending operator reconciliation.'
            )
        if isinstance(known_good, dict) and known_good.get('deployment_url'):
            phase = 'resume_reconciliation'
            tell({'stage': '기존 운영 배포 재확인 중', 'completed': 0, 'total': 5, 'unit': '공정 완료'})
            current = _inspect_production(executable, production_mcp_url, stage, run)
            report['resume_reconciliation'] = current
            state = transaction.get('state')
            interrupted_staged = transaction.get('staged_url')
            if state in _INTERRUPTED_PROMOTION_STATES and interrupted_staged:
                if current.get('deployment_url') == interrupted_staged:
                    if not _rollback_known_good(
                            report=report, output=output, transaction=transaction,
                            executable=executable, production_mcp_url=production_mcp_url,
                            stage=stage, run=run, known_good=known_good,
                            expected_current=current, builder_context=builder_context, guard=guard):
                        raise ReleaseError(
                            'Interrupted promotion reached production and rollback could not be confirmed.'
                        )
                elif not _same_deployment(known_good, current):
                    _deployment_checkpoint(
                        report, output, transaction, 'production_diverged',
                        observed_production=current,
                    )
                    raise ReleaseError(
                        'Production changed after an interrupted deployment; automatic retry is blocked.'
                    )
            elif not _same_deployment(known_good, current):
                _deployment_checkpoint(
                    report, output, transaction, 'production_diverged',
                    observed_production=current,
                )
                raise ReleaseError(
                    'Production no longer matches the recorded known-good deployment; '
                    'automatic retry is blocked.'
                )
            previous_production = known_good
        else:
            phase = 'capture_previous_production'
            tell({'stage': '기존 운영 배포 기록 중', 'completed': 0, 'total': 5, 'unit': '공정 완료'})
            previous_production = _inspect_production(executable, production_mcp_url, stage, run)
            transaction = {
                'schema': 'ncs_builder_deployment_transaction_v1',
                'started_at': started_at,
                'known_good_production': previous_production,
            }
        report['previous_production'] = previous_production
        _deployment_checkpoint(
            report, output, transaction, 'staging_pending',
            known_good_production=previous_production,
        )
        phase = 'staging'
        tell({'stage': '검증된 Vercel 빌드 업로드 중', 'completed': 1, 'total': 5, 'unit': '공정 완료'})
        stdout = run([
            executable, 'deploy', '--prebuilt', '--prod', '--skip-domain', '--yes'
        ], stage)
        if _tree_evidence(prebuilt_output) != report['prebuilt_output']:
            raise ReleaseError('Vercel prebuilt output changed during upload.')
        urls = list(dict.fromkeys(re.findall(r'https://[A-Za-z0-9-]+\.vercel\.app', stdout)))
        if len(urls) != 1:
            raise ReleaseError('Vercel did not return exactly one staging deployment URL.')
        staged = urls[0]
        production_root = _production_root(production_mcp_url)
        expected_prefix = report['project']['projectName'].lower() + '-'
        staged_host = staged.removeprefix('https://').lower()
        if (staged == production_root
                or staged == previous_production.get('deployment_url')
                or not staged_host.startswith(expected_prefix)):
            raise ReleaseError('Vercel staging URL is not a unique deployment for the selected project.')
        report['staged_url'] = staged
        _deployment_checkpoint(report, output, transaction, 'staged_deployed', staged_url=staged)
        phase = 'staged'
        tell({'stage': '검증용 health·ready·MCP 확인 중', 'completed': 2, 'total': 5, 'unit': '공정 완료'})
        evidence = verify(staged + '/api/mcp', report['build_id'])
        report['staged_verification'] = evidence
        if not evidence.get('ok'):
            raise ReleaseError('Staged health, readiness, MCP, or exact build identity verification failed.')
        _deployment_checkpoint(report, output, transaction, 'staged_verified', staged_url=staged)
        phase = 'reconfirm_previous_production'
        reconfirmed = _inspect_production(executable, production_mcp_url, stage, run)
        report['production_reconfirmation'] = reconfirmed
        if not _same_deployment(previous_production, reconfirmed):
            raise ReleaseError('Production changed during staging; refusing to promote over a newer deployment.')
        phase = 'promotion'
        tell({'stage': '검증된 배포를 운영 주소에 연결 중', 'completed': 3, 'total': 5, 'unit': '공정 완료'})
        _deployment_checkpoint(report, output, transaction, 'promotion_pending', staged_url=staged)
        run([executable, 'promote', staged, '--yes'], stage)
        report['promotion_performed'] = True
        _deployment_checkpoint(report, output, transaction, 'promotion_acknowledged')
        phase = 'production'
        promoted = _inspect_production(executable, production_mcp_url, stage, run)
        report['production_after_promotion'] = promoted
        if promoted.get('deployment_url') != staged:
            raise ReleaseError('Production alias does not resolve to the verified staged deployment.')
        _deployment_checkpoint(
            report, output, transaction, 'production_verification_pending',
            observed_production=promoted,
        )
        tell({'stage': '운영 health·ready·MCP 확인 중', 'completed': 4, 'total': 5, 'unit': '공정 완료'})
        evidence = verify(production_mcp_url, report['build_id'])
        report['production_verification'] = evidence
        if not evidence.get('ok'):
            raise ReleaseError('Production health, readiness, MCP, or build identity verification failed.')
        transaction['state'] = 'deployed'
        transaction['updated_at'] = datetime.now(timezone.utc).isoformat()
        report.update(ok=True, status='deployed', production_mcp_url=production_mcp_url)
        tell({'stage': 'Vercel MCP 업데이트 완료', 'completed': 5, 'total': 5, 'unit': '공정 완료'})
    except BuilderAuthorizationError:
        raise
    except Exception as exc:
        public_error = (str(exc) if isinstance(exc, ReleaseError)
                        else 'Release operation failed; baseline remains unchanged.')
        if (phase == 'promotion' and previous_production is not None
                and stage is not None and staged is not None and transaction is not None):
            report['promotion_outcome_reconfirmation_attempted'] = True
            try:
                observed = _inspect_production(executable, production_mcp_url, stage, run)
                report['promotion_outcome_reconfirmation'] = observed
                if observed.get('deployment_url') == staged:
                    report['promotion_performed'] = True
                    report['production_after_promotion'] = observed
                    phase = 'production'
                elif _same_deployment(previous_production, observed):
                    report['promotion_performed'] = False
                    _deployment_checkpoint(
                        report, output, transaction, 'failed_before_promotion',
                        observed_production=observed,
                    )
                else:
                    _deployment_checkpoint(
                        report, output, transaction, 'production_diverged',
                        observed_production=observed,
                    )
            except BuilderAuthorizationError:
                raise
            except Exception:
                _deployment_checkpoint(
                    report, output, transaction, 'promotion_outcome_unconfirmed'
                )
                public_error += ' Promotion outcome could not be confirmed.'
        expected_current = report.get('production_after_promotion')
        if (phase == 'production' and report.get('promotion_performed') is True
                and previous_production is not None and stage is not None
                and transaction is not None and not isinstance(expected_current, dict)):
            try:
                observed = _inspect_production(executable, production_mcp_url, stage, run)
                report['promotion_outcome_reconfirmation'] = observed
                if _same_deployment(previous_production, observed):
                    report['rollback_attempted'] = False
                    report['rollback_performed'] = False
                    report['rollback_reconfirmed'] = True
                    _deployment_checkpoint(
                        report, output, transaction, 'rollback_confirmed',
                        rollback_already_restored=True,
                    )
                    report['promotion_performed'] = False
                elif (observed.get('deployment_url') == staged
                      and observed.get('deployment_id')):
                    expected_current = observed
                    report['production_after_promotion'] = observed
                else:
                    _deployment_checkpoint(
                        report, output, transaction, 'production_diverged',
                        observed_production=observed,
                    )
            except BuilderAuthorizationError:
                raise
            except Exception:
                _deployment_checkpoint(
                    report, output, transaction, 'promotion_outcome_unconfirmed'
                )
        if (phase == 'production' and report.get('promotion_performed') is True
                and previous_production is not None and stage is not None
                and transaction is not None and isinstance(expected_current, dict)
                and transaction.get('state') != 'production_diverged'):
            _rollback_known_good(
                report=report, output=output, transaction=transaction,
                executable=executable, production_mcp_url=production_mcp_url,
                stage=stage, run=run, known_good=previous_production,
                expected_current=expected_current, builder_context=builder_context, guard=guard,
            )
            if (report.get('rollback_reconfirmed')
                    and transaction.get('rollback_already_restored')):
                public_error += ' Previous production deployment was already restored.'
            elif report.get('rollback_reconfirmed'):
                public_error += ' Previous production deployment was explicitly restored.'
            elif (report.get('rollback_attempted') is False
                  and transaction.get('state') == 'production_diverged'):
                public_error += ' Production diverged; rollback was not attempted.'
            elif report.get('rollback_attempted') is False:
                public_error += ' Rollback precondition could not be confirmed; rollback was not attempted.'
            else:
                public_error += ' Explicit rollback was attempted but could not be confirmed.'
        elif transaction is not None and transaction.get('state') in _INTERRUPTED_PROMOTION_STATES:
            transaction['state'] = 'promotion_outcome_unconfirmed'
            transaction['updated_at'] = datetime.now(timezone.utc).isoformat()
        elif transaction is not None and transaction.get('state') not in (
                _UNCONFIRMED_REMOTE_STATES | {'rollback_confirmed', 'deployed'}):
            transaction['state'] = 'failed_before_promotion'
            transaction['updated_at'] = datetime.now(timezone.utc).isoformat()
        report.update(ok=False, status='deploy_failed', failed_phase=phase,
                      error=public_error)
    history.append({'attempt': len(history) + 1, 'started_at': started_at,
                    'finished_at': datetime.now(timezone.utc).isoformat(),
                    **{key: report[key] for key in attempt_fields if key in report}})
    write(output, report)
    return report
