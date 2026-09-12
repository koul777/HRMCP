"""Restartable validation of an isolated Builder candidate, bound to file bytes."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Callable

from .api_refresh_builder import file_sha256
from .builder_authorization import (
    BuilderAuthorizationError, BuilderOperationContext, require_builder_context,
)
from .ontology_refresh_builder import validate_ontology_database

TABLES = (
    'competency_units', 'competency_elements', 'performance_criteria', 'ksa_items',
    'ontology_concepts', 'ncs_training_courses', 'ncs_qualification_items',
    'ncs_job_base_competencies', 'ncs_career_paths',
)
SCHEMA = 'ncs_builder_validation_checkpoint_v1'


def _signature(db: Path) -> tuple:
    result = []
    for path in (db, Path(str(db) + '-wal')):
        try:
            stat = path.stat()
            result.append(None if path != db and stat.st_size == 0 else (stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            result.append(None)
    return tuple(result)


def _atomic_checkpoint(path: Path, payload: dict,
                       authorize: Callable[[], None]) -> None:
    authorize()
    path.parent.mkdir(parents=True, exist_ok=True)
    authorize()
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        authorize()
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _foreign_keys(db: Path) -> None:
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('DB 관계 무결성 검증에 실패했습니다.')


def _count(db: Path, table: str) -> int:
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (table,)).fetchone()
        return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] if exists else 0


def _require_candidate_paths(db: Path, checkpoint_path: Path,
                             builder_context: BuilderOperationContext) -> None:
    context = require_builder_context(
        builder_context, action=("build_delta", "refresh_api", "copy_current", "resume"),
        version_dir=db.parent,
    )
    expected_dir = Path(context.state_dir) / 'versions' / context.version
    if (db.name != 'ncs.db' or db.resolve() != expected_dir / 'ncs.db'
            or checkpoint_path.name != 'validation-checkpoint.json'
            or checkpoint_path.parent != db.parent
            or checkpoint_path.resolve() != expected_dir / 'validation-checkpoint.json'):
        raise BuilderAuthorizationError('Validation paths must be the exact Builder candidate and checkpoint.')
    # Reject file-level redirects too: a hardlink preserves resolve() yet shares
    # writable bytes with a different database. Sidecars can redirect WAL writes.
    for path in (db.parent, db, checkpoint_path, Path(str(db) + '-wal'),
                 Path(str(db) + '-shm'), Path(str(db) + '-journal')):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or (path != db.parent and (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1))):
            raise BuilderAuthorizationError('Validation paths must not be links or reparse points.')


def validate_candidate(db: Path, checkpoint_path: Path, progress: Callable | None = None,
                       *, builder_context: BuilderOperationContext) -> dict:
    """Resume completed checks only after hashing the exact candidate again.

    Checkpoints describe validation evidence only. They never authorize publishing
    or human review status changes. Cancellation propagates without discarding
    completed checks, while a changed candidate invalidates previous evidence.
    """
    db = Path(os.path.abspath(db))
    checkpoint_path = Path(os.path.abspath(checkpoint_path))

    def authorize() -> None:
        _require_candidate_paths(db, checkpoint_path, builder_context)

    authorize()
    if not db.is_file():
        raise FileNotFoundError(f'Builder candidate database does not exist: {db}')
    emit = progress or (lambda event: None)
    emit({'stage': '검증 준비 · WAL 반영', 'completed': 0, 'total': None, 'unit': '검사'})
    authorize()
    with closing(sqlite3.connect(db.as_uri() + '?mode=rw', uri=True)) as conn:
        authorize()
        checkpoint = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint and checkpoint[0]:
            raise ValueError('작업 DB가 사용 중입니다. 검증을 다시 시도하세요.')
    signature = _signature(db)
    identity = file_sha256(db, progress, '검증 재개용 DB 해시 확인')
    if signature != _signature(db):
        raise ValueError('해시 검사 중 DB가 변경되었습니다.')
    state = {}
    try:
        state = json.loads(checkpoint_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass
    if not isinstance(state, dict) or state.get('schema') != SCHEMA or state.get('sha256') != identity:
        state = {'schema': SCHEMA, 'sha256': identity, 'counts': {}}
    if not isinstance(state.get('counts'), dict):
        state['counts'] = {}

    def save() -> None:
        authorize()
        if signature != _signature(db):
            raise ValueError('검증 중 DB가 변경되었습니다. 다시 검증하세요.')
        _atomic_checkpoint(checkpoint_path, state, authorize)

    total = 2 + len(TABLES) + 1
    completed = 0

    def event(label: str, done: bool = False) -> None:
        nonlocal completed
        if done:
            completed += 1
        emit({'stage': label, 'completed': completed, 'total': total, 'unit': '검증 항목'})

    event('온톨로지 구조 검증')
    if not isinstance(state.get('validation'), dict) or not state['validation'].get('ok'):
        validation = validate_ontology_database(db)
        if not validation.get('ok'):
            raise ValueError('온톨로지 검증에 실패했습니다. MCP에 반영할 수 없습니다.')
        state['validation'] = validation
        save()
    event('온톨로지 구조 검증 완료', True)
    event('DB 참조 무결성 검증')
    if state.get('foreign_key_check') != 'ok':
        _foreign_keys(db)
        state['foreign_key_check'] = 'ok'
        save()
    event('DB 참조 무결성 검증 완료', True)
    for table in TABLES:
        event(f'테이블 건수 확인 · {table}')
        if type(state['counts'].get(table)) is not int or state['counts'][table] < 0:
            state['counts'][table] = _count(db, table)
            save()
        event(f'테이블 건수 확인 완료 · {table}', True)
    if any(state['counts'][table] == 0 for table in ('competency_units', 'performance_criteria', 'ksa_items')):
        raise ValueError('필수 원천 테이블이 비어 있어 MCP 반영을 차단했습니다.')
    event('완성 DB 동일성 검증')
    final_hash = file_sha256(db, progress, '완성 DB 최종 해시 확인')
    if final_hash != identity or signature != _signature(db):
        raise ValueError('검증 중 DB가 변경되었습니다. 다시 검증하세요.')
    state.update(final_sha256=final_hash, bytes=db.stat().st_size, complete=True)
    authorize()
    save()
    event('완성 DB 동일성 검증 완료', True)
    return {'validation': state['validation'], 'counts': {table: state['counts'][table] for table in TABLES},
            'sha256': final_hash, 'bytes': state['bytes']}
