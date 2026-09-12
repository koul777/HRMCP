"""Process-local Builder capabilities bound to the exclusive on-disk operation.

This guards application entry points, not hostile Python code in this process.
Serialized lineage, environment variables and copied contexts confer no authority.
Only DataBuilder.exclusive uses the private issuance context manager.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


OPERATION_SCHEMA = "ncs_data_builder_operation_v1"
BUILDER_OWNER = "windows_ncs_data_builder"


class BuilderAuthorizationError(RuntimeError):
    """A mutation lacks a matching live Builder operation."""


@dataclass(frozen=True, eq=False)
class BuilderOperationContext:
    schema: str
    operation_id: str
    owner: str
    action: str
    version: str | None
    root: str
    state_dir: str
    nonce_sha256: str
    pid: int
    started_at: str
    _nonce: str = field(repr=False)

    def lineage(self) -> dict:
        """Non-authorizing evidence safe to persist in build reports."""
        return {
            key: getattr(self, key)
            for key in (
                "schema", "operation_id", "owner", "action", "version", "root",
                "state_dir", "nonce_sha256", "pid", "started_at",
            )
        }


_registry_lock = threading.RLock()
_active: dict[str, tuple[BuilderOperationContext, dict]] = {}
_lock_identities: dict[str, tuple[int, int]] = {}
_current: ContextVar[BuilderOperationContext | None] = ContextVar(
    "ncs_builder_operation", default=None
)


def current_builder_context() -> BuilderOperationContext | None:
    """Diagnostic accessor; callers must still pass and validate the capability."""
    return _current.get()


QUALIFICATION_OPERATOR_COMMANDS = frozenset({
    "collect-qualification-items", "retry-qualification-errors",
})


@contextmanager
def qualification_operator_lease(*, root: Path, command: str):
    """Serialize an already preflighted operator batch with DataBuilder writes.

    The yielded record is evidence only, never a Builder capability. No version,
    approval record, or Builder current-context is created by this exception.
    Callers must validate the operational gate and bounded arguments first.
    """
    if command not in QUALIFICATION_OPERATOR_COMMANDS:
        raise BuilderAuthorizationError("Command is outside the qualification operator exception.")
    root_key = _canonical(root)
    state_dir = Path(root_key) / ".state/ncs-data-builder"
    validate_builder_paths(root, Path(root) / ".state/ncs-data-builder")
    state_key = _canonical(state_dir)
    nonce = secrets.token_hex(32)
    lineage = {
        "schema": "ncs_qualification_operator_operation_v1",
        "operation_id": uuid.uuid4().hex,
        "owner": "qualification_operator",
        "action": command,
        "root": root_key,
        "state_dir": state_key,
        "nonce_sha256": hashlib.sha256(nonce.encode("ascii")).hexdigest(),
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    lock = state_dir / "operation.lock"
    with _registry_lock:
        if state_key in _active or _current.get() is not None:
            raise BuilderAuthorizationError("Another Builder operation is already active.")
        state_dir.mkdir(parents=True, exist_ok=True)
        validate_builder_paths(root_key, state_dir)
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise BuilderAuthorizationError("Another operation holds operation.lock.") from exc
        identity = _identity(os.fstat(fd))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(lineage, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            _release_owned_lock(lock, lineage, identity)
            raise
    try:
        yield dict(lineage)
    finally:
        with _registry_lock:
            _release_owned_lock(lock, lineage, identity)


def _canonical(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def validate_builder_paths(root: str | Path, state_dir: str | Path,
                           version_dir: str | Path | None = None) -> None:
    """Check lexical placement and resolved placement independently.

    State paths may not redirect through symlinks, junctions or other reparse
    points, even to a different location inside the repository.
    """
    lexical_root = Path(os.path.abspath(Path(root).expanduser()))
    expected = lexical_root / ".state" / "ncs-data-builder"
    lexical_state = Path(os.path.abspath(Path(state_dir).expanduser()))
    if os.path.normcase(str(lexical_state)) != os.path.normcase(str(expected)):
        raise BuilderAuthorizationError("Builder state directory must belong to its root.")
    canonical_root = Path(_canonical(lexical_root))
    for path in (lexical_root / ".state", expected, expected / "versions"):
        wanted = canonical_root / path.relative_to(lexical_root)
        if _canonical(path) != os.path.normcase(str(wanted)):
            raise BuilderAuthorizationError("Builder state path redirects outside its authorized location.")
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise BuilderAuthorizationError("Builder state path is a reparse point.")
    if version_dir is not None:
        lexical_version = Path(os.path.abspath(version_dir))
        try:
            relative = lexical_version.relative_to(expected / "versions")
        except ValueError as exc:
            raise BuilderAuthorizationError("Builder version escapes the state directory.") from exc
        if len(relative.parts) != 1 or _canonical(lexical_version) != os.path.normcase(
            str(canonical_root / ".state/ncs-data-builder/versions" / relative)
        ):
            raise BuilderAuthorizationError("Builder version directory redirects outside its scope.")


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _read_owned_lock(path: Path, identity: tuple[int, int]) -> dict:
    try:
        with path.open("r", encoding="utf-8") as stream:
            if _identity(os.fstat(stream.fileno())) != identity:
                raise BuilderAuthorizationError("Builder lock file identity changed.")
            payload = json.loads(stream.read(16_385))
        if _identity(path.lstat()) != identity or not isinstance(payload, dict):
            raise BuilderAuthorizationError("Builder lock file identity changed.")
        return payload
    except (OSError, ValueError, TypeError) as exc:
        raise BuilderAuthorizationError("Builder operation lock is missing or invalid.") from exc


def _claim_owned_lock(lock: Path, payload: dict, identity: tuple[int, int]) -> Path:
    """Atomically detach the lock, then verify ownership before any mutation.

    A competing replacement is retained, never unlinked or overwritten. The
    random handoff name avoids a read/unlink race on the public lock name.
    This is not a defence against a hostile writer racing private names or
    replacing ancestor directories; filesystem access control remains required.
    """
    if _read_owned_lock(lock, identity) != payload:
        raise BuilderAuthorizationError("Builder lock token changed.")
    claimed = lock.with_name("operation-handoff-" + uuid.uuid4().hex + ".lock")
    os.rename(lock, claimed)
    try:
        if _read_owned_lock(claimed, identity) != payload:
            raise BuilderAuthorizationError("Builder lock changed during ownership handoff.")
    except BuilderAuthorizationError:
        # Exclusive link creation restores the name only if nobody acquired it.
        # Keep the handoff evidence, including an unexpected replacement inode.
        try:
            os.link(claimed, lock)
        except OSError:
            pass
        raise
    return claimed


def _release_owned_lock(lock: Path, payload: dict, identity: tuple[int, int]) -> None:
    try:
        claimed = _claim_owned_lock(lock, payload, identity)
        if _read_owned_lock(claimed, identity) == payload:
            claimed.unlink()
    except (BuilderAuthorizationError, OSError):
        pass


def _read_lock(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.loads(stream.read(16_385))
        if not isinstance(payload, dict):
            raise ValueError("invalid operation lock")
        return payload
    except (OSError, ValueError, TypeError) as exc:
        raise BuilderAuthorizationError("Builder operation lock is missing or invalid.") from exc


def require_builder_context(
    builder_context: BuilderOperationContext | None,
    *,
    action: str | tuple[str, ...],
    root: str | Path | None = None,
    state_dir: str | Path | None = None,
    version: str | None = None,
    version_dir: str | Path | None = None,
) -> BuilderOperationContext:
    """Validate the issued object, nonce, full lock record and requested scope.

    No implicit current-context fallback is allowed. ``version_dir`` must be
    exactly this operation's version folder, not just any path below the root.
    """
    if type(builder_context) is not BuilderOperationContext:
        raise BuilderAuthorizationError("A live DataBuilder operation is required.")
    context = builder_context
    with _registry_lock:
        validate_builder_paths(context.root, context.state_dir)
        registered = _active.get(context.state_dir)
        if registered is None or registered[0] is not context:
            raise BuilderAuthorizationError("Builder capability was not issued or has expired.")
        payload = context.lineage()
        if (
            payload != registered[1]
            or context.schema != OPERATION_SCHEMA
            or context.owner != BUILDER_OWNER
            or context.pid != os.getpid()
            or not hmac.compare_digest(
                hashlib.sha256(context._nonce.encode("ascii")).hexdigest(),
                context.nonce_sha256,
            )
        ):
            raise BuilderAuthorizationError("Builder capability identity does not match.")
        allowed = (action,) if isinstance(action, str) else tuple(action)
        if context.action not in allowed:
            raise BuilderAuthorizationError("Builder operation action does not match.")
        if root is not None and _canonical(root) != context.root:
            raise BuilderAuthorizationError("Builder operation root does not match.")
        if state_dir is not None and _canonical(state_dir) != context.state_dir:
            raise BuilderAuthorizationError("Builder operation state directory does not match.")
        if version is not None and version != context.version:
            raise BuilderAuthorizationError("Builder operation version does not match.")
        if version_dir is not None and (
            context.version is None
            or _canonical(version_dir)
            != _canonical(Path(context.state_dir) / "versions" / context.version)
        ):
            raise BuilderAuthorizationError("Builder operation version directory does not match.")
        if version_dir is not None:
            # Permit a caller's 8.3 spelling of the same repository root, but
            # not an arbitrary symlink alias located outside its state tree.
            supplied = Path(os.path.abspath(version_dir))
            supplied_root = supplied.parent.parent.parent.parent
            if _canonical(supplied_root) != context.root:
                raise BuilderAuthorizationError("Builder version lexical root does not match.")
            validate_builder_paths(supplied_root,
                                   supplied_root / ".state/ncs-data-builder", supplied)
        if context.version is not None:
            validate_builder_paths(context.root, context.state_dir,
                                   Path(context.state_dir) / "versions" / context.version)
        if _read_owned_lock(Path(context.state_dir) / "operation.lock",
                            _lock_identities[context.state_dir]) != payload:
            raise BuilderAuthorizationError("Builder operation lock identity does not match.")
    return context


def _bind_operation_version(context: BuilderOperationContext, version: str) -> None:
    """Bind a new build version once while retaining the issued object identity."""
    if not version or any(c not in "0123456789abcdef_-" for c in version):
        raise BuilderAuthorizationError("Invalid Builder version.")
    with _registry_lock:
        require_builder_context(context, action=context.action)
        if context.version is not None:
            if context.version != version:
                raise BuilderAuthorizationError("Builder operation version is already bound.")
            return
        payload = context.lineage()
        payload["version"] = version
        lock = Path(context.state_dir) / "operation.lock"
        identity = _lock_identities[context.state_dir]
        claimed = _claim_owned_lock(lock, context.lineage(), identity)
        with claimed.open("r+", encoding="utf-8") as stream:
            if _identity(os.fstat(stream.fileno())) != identity:
                raise BuilderAuthorizationError("Builder lock identity changed before binding.")
            if json.load(stream) != context.lineage():
                raise BuilderAuthorizationError("Builder operation lock changed before binding.")
            stream.seek(0)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(claimed, lock)
        except OSError as exc:
            raise BuilderAuthorizationError("Builder lock reacquisition failed during binding.") from exc
        if _read_owned_lock(lock, identity) != payload:
            raise BuilderAuthorizationError("Builder lock changed after binding handoff.")
        if _read_owned_lock(claimed, identity) == payload:
            claimed.unlink()
        object.__setattr__(context, "version", version)
        _active[context.state_dir] = (context, payload)


@contextmanager
def _exclusive_operation(*, root: Path, state_dir: Path, action: str, version: str | None):
    """Private issuer used only by DataBuilder.exclusive."""
    if not isinstance(action, str) or not action.strip():
        raise BuilderAuthorizationError("A concrete Builder action is required.")
    if version is not None and (
        not version or any(c not in "0123456789abcdef_-" for c in version)
    ):
        raise BuilderAuthorizationError("Invalid Builder version.")
    validate_builder_paths(root, state_dir,
                           Path(state_dir) / "versions" / version if version else None)
    root_key, state_key = _canonical(root), _canonical(state_dir)
    if state_key != _canonical(Path(root_key) / ".state/ncs-data-builder"):
        raise BuilderAuthorizationError("Builder state directory must belong to its root.")
    nonce = secrets.token_hex(32)
    context = BuilderOperationContext(
        schema=OPERATION_SCHEMA, operation_id=uuid.uuid4().hex, owner=BUILDER_OWNER,
        action=action, version=version, root=root_key, state_dir=state_key,
        nonce_sha256=hashlib.sha256(nonce.encode("ascii")).hexdigest(),
        pid=os.getpid(), started_at=datetime.now(timezone.utc).isoformat(), _nonce=nonce,
    )
    lock = Path(state_key) / "operation.lock"
    with _registry_lock:
        if state_key in _active or _current.get() is not None:
            raise BuilderAuthorizationError("Another Builder operation is already active.")
        # Exclusive creation also rejects legacy PID-only and stale lock files.
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise BuilderAuthorizationError("Another Builder operation holds operation.lock.") from exc
        identity = _identity(os.fstat(fd))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(context.lineage(), stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            # Only remove a complete record attributable to this operation.
            _release_owned_lock(lock, context.lineage(), identity)
            raise
        _active[state_key] = (context, context.lineage())
        _lock_identities[state_key] = identity
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)
        with _registry_lock:
            _active.pop(state_key, None)
            _lock_identities.pop(state_key, None)
            _release_owned_lock(lock, context.lineage(), identity)
