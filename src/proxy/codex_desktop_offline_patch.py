"""Maintain the local Codex Desktop offline-auth compatibility patch."""

from __future__ import annotations

import hashlib
import ctypes
import fcntl
import json
import mmap
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


ORIGINAL_AUTH_EXPRESSION = b"T=y?!r&&h?.hasChatGptToken:void 0"
LEGACY_PATCHED_AUTH_EXPRESSION = b"T=y&&(h?.hasChatGptToken||void 0)"
PATCHED_AUTH_EXPRESSION = b"T=y&&h?.hasChatGptToken||void 0  "
_MANIFEST_NAME = "manifest.json"
_SAFE_VERSION_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_CODEX_BUNDLE_ID = "com.openai.codex"
_AT_FDCWD = -2
_RENAME_SWAP = 0x00000002
_OPENAI_DESIGNATED_REQUIREMENT = (
    f'identifier "{_CODEX_BUNDLE_ID}" and anchor apple generic and '
    "certificate 1[field.1.2.840.113635.100.6.2.6] /* exists */ and "
    "certificate leaf[field.1.2.840.113635.100.6.1.13] /* exists */ and "
    'certificate leaf[subject.OU] = "2DC432GLL2"'
)


class PatchError(RuntimeError):
    pass


class _PreserveStagingError(PatchError):
    pass


class _BundleRollbackError(_PreserveStagingError):
    pass


@dataclass(frozen=True)
class PatchResult:
    changed: bool
    backup_path: Path | None


@dataclass(frozen=True)
class _BundleIdentity:
    version: str
    build: str
    bundle_device: int
    bundle_inode: int
    archive_device: int
    archive_inode: int
    archive_size: int
    archive_hash: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_offsets(mapped: mmap.mmap, needle: bytes, limit: int = 2) -> list[int]:
    offsets: list[int] = []
    start = 0
    while len(offsets) < limit:
        offset = mapped.find(needle, start)
        if offset < 0:
            break
        offsets.append(offset)
        start = offset + len(needle)
    return offsets


def _archive_state(archive: Path) -> str:
    if not archive.is_file():
        raise PatchError(f"Codex Desktop archive does not exist: {archive}")
    if len(ORIGINAL_AUTH_EXPRESSION) != len(PATCHED_AUTH_EXPRESSION):
        raise PatchError("Offline-auth patch must preserve the ASAR byte length")
    if archive.stat().st_size == 0:
        raise PatchError(
            "Unsupported Codex Desktop build: expected exactly one offline-auth "
            "expression"
        )

    with archive.open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            original_offsets = _find_offsets(mapped, ORIGINAL_AUTH_EXPRESSION)
            patched_offsets = _find_offsets(mapped, PATCHED_AUTH_EXPRESSION)
            legacy_offsets = _find_offsets(mapped, LEGACY_PATCHED_AUTH_EXPRESSION)

    if len(original_offsets) == 1 and not patched_offsets and not legacy_offsets:
        return "unpatched"
    if not original_offsets and len(patched_offsets) == 1 and not legacy_offsets:
        return "patched"
    if not original_offsets and not patched_offsets and len(legacy_offsets) == 1:
        return "legacy-patched"
    raise PatchError(
        "Unsupported Codex Desktop build: expected exactly one offline-auth "
        "expression"
    )


def _read_manifest(backup_dir: Path) -> dict[str, str]:
    path = backup_dir / _MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PatchError(f"Invalid backup manifest: {path}") from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    ):
        raise PatchError(f"Invalid backup manifest: {path}")
    return value


def _write_manifest(backup_dir: Path, manifest: dict[str, str]) -> None:
    path = backup_dir / _MANIFEST_NAME
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".manifest.", suffix=".tmp", dir=backup_dir
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _ensure_archive_backup(source: Path, backups: Path, archive_name: str) -> Path:
    backups.mkdir(parents=True, exist_ok=True)
    original_hash = _sha256(source)
    backup_path = backups / f"{archive_name}.{original_hash}.backup"
    if backup_path.exists():
        if not backup_path.is_file() or _sha256(backup_path) != original_hash:
            raise PatchError(f"Existing archive backup failed integrity: {backup_path}")
        return backup_path

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive_name}.", suffix=".partial", dir=backups
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        if _sha256(temporary) != original_hash:
            raise PatchError(f"Archive backup failed integrity: {temporary}")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        temporary.replace(backup_path)
    finally:
        temporary.unlink(missing_ok=True)
    return backup_path


def _recorded_archive_backup(backups: Path, patched_hash: str) -> Path:
    manifest = _read_manifest(backups)
    backup_name = manifest.get(patched_hash)
    if not backup_name:
        raise PatchError("Patched archive has no recorded original backup")
    if Path(backup_name).name != backup_name:
        raise PatchError(f"Invalid manifest backup path: {backup_name}")
    backup_path = backups / backup_name
    if not backup_path.is_file():
        raise PatchError(f"Recorded backup is missing: {backup_path}")
    expected_name = f"app.asar.{_sha256(backup_path)}.backup"
    if backup_path.name != expected_name or _archive_state(backup_path) != "unpatched":
        raise PatchError(f"Recorded archive backup failed integrity: {backup_path}")
    return backup_path


def apply_archive_patch(
    archive_path, backup_dir, original_backup_path=None
) -> PatchResult:
    archive = Path(archive_path)
    backups = Path(backup_dir)
    state = _archive_state(archive)

    if state == "patched":
        backup_path = _recorded_archive_backup(backups, _sha256(archive))
        return PatchResult(changed=False, backup_path=backup_path)

    backup_source = archive
    if state == "legacy-patched":
        if original_backup_path is None:
            raise PatchError("Legacy offline-auth patch requires an original backup")
        backup_source = Path(original_backup_path)
        if _archive_state(backup_source) != "unpatched":
            raise PatchError("Legacy offline-auth patch backup is not original")
    backup_path = _ensure_archive_backup(backup_source, backups, archive.name)

    with archive.open("r+b") as handle:
        with mmap.mmap(handle.fileno(), 0) as mapped:
            original_offsets = _find_offsets(mapped, ORIGINAL_AUTH_EXPRESSION)
            patched_offsets = _find_offsets(mapped, PATCHED_AUTH_EXPRESSION)
            legacy_offsets = _find_offsets(mapped, LEGACY_PATCHED_AUTH_EXPRESSION)
            expected_offsets = (
                original_offsets if state == "unpatched" else legacy_offsets
            )
            if len(expected_offsets) != 1 or patched_offsets:
                raise PatchError(
                    "Unsupported Codex Desktop build: expected exactly one known "
                    "offline-auth expression"
                )
            needle = (
                ORIGINAL_AUTH_EXPRESSION
                if state == "unpatched"
                else LEGACY_PATCHED_AUTH_EXPRESSION
            )
            offset = expected_offsets[0]
            mapped[offset : offset + len(needle)] = PATCHED_AUTH_EXPRESSION
            mapped.flush()

    patched_hash = _sha256(archive)
    manifest = _read_manifest(backups)
    manifest[patched_hash] = backup_path.name
    _write_manifest(backups, manifest)
    return PatchResult(changed=True, backup_path=backup_path)


def restore_archive(archive_path, backup_path):
    archive = Path(archive_path)
    backup = Path(backup_path)
    if not backup.is_file():
        raise PatchError(f"Backup archive does not exist: {backup}")
    backup_hash = _sha256(backup)
    if (
        backup.name != f"{archive.name}.{backup_hash}.backup"
        or _archive_state(backup) != "unpatched"
    ):
        raise PatchError(f"Backup is not a verified content-addressed archive: {backup}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive.name}.", suffix=".restore", dir=archive.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(backup, temporary)
        if _sha256(temporary) != backup_hash or _archive_state(temporary) != "unpatched":
            raise PatchError(f"Restored archive failed integrity: {temporary}")
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_full_backup(backup: Path, version: str, build: str) -> None:
    archive = backup / "Contents" / "Resources" / "app.asar"
    try:
        actual_version, actual_build = _read_desktop_metadata(backup)
    except PatchError as exc:
        raise PatchError(f"Invalid full application backup: {backup}") from exc
    if (
        actual_version != version
        or actual_build != build
        or _archive_state(archive) != "unpatched"
    ):
        raise PatchError(f"Invalid full application backup: {backup}")


def _exchange_paths(left: Path, right: Path) -> None:
    if sys.platform == "darwin":
        renameatx_np = ctypes.CDLL(None, use_errno=True).renameatx_np
        renameatx_np.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameatx_np.restype = ctypes.c_int
        result = renameatx_np(
            _AT_FDCWD,
            os.fsencode(left),
            _AT_FDCWD,
            os.fsencode(right),
            _RENAME_SWAP,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
        return

    temporary = left.with_name(f".{left.name}.exchange")
    stage = 0
    left.replace(temporary)
    stage = 1
    try:
        right.replace(left)
        stage = 2
        temporary.replace(right)
    except Exception:
        if stage == 2 and left.exists() and not right.exists():
            left.replace(right)
        if stage >= 1 and temporary.exists() and not left.exists():
            temporary.replace(left)
        raise


def _activate_staged_bundle(
    app: Path,
    staged_app: Path,
    expected_live: _BundleIdentity,
    expected_staged: _BundleIdentity,
    operation: str,
    command_runner,
) -> None:
    _exchange_paths(app, staged_app)
    try:
        installed_identity = _bundle_identity(app)
    except Exception as exc:
        try:
            displaced_identity = _bundle_identity(staged_app)
            if displaced_identity != expected_live:
                _verify_openai_signed_bundle(
                    staged_app,
                    command_runner,
                    "Displaced application failed OpenAI signature verification",
                )
        except Exception as displaced_exc:
            raise _PreserveStagingError(
                f"Codex Desktop became unreadable after {operation}: {exc}; "
                f"the displaced bundle is also unsafe: {displaced_exc}; "
                f"recovery bundle preserved at {staged_app}"
            ) from displaced_exc
        try:
            if app.exists():
                _exchange_paths(app, staged_app)
            else:
                staged_app.replace(app)
        except Exception as rollback_exc:
            raise _BundleRollbackError(
                f"Codex Desktop became unreadable after {operation}: {exc}; "
                f"rollback failed: {rollback_exc}"
            ) from rollback_exc
        if staged_app.exists():
            raise _PreserveStagingError(
                f"Codex Desktop became unreadable after {operation}; "
                "the prior bundle was restored and the failed replacement was preserved"
            ) from exc
        raise PatchError(
            f"Codex Desktop became unreadable after {operation}; "
            "the prior bundle was restored"
        ) from exc
    if installed_identity != expected_staged:
        try:
            _verify_openai_signed_bundle(
                app,
                command_runner,
                "Replacement application failed OpenAI signature verification",
            )
        except PatchError as signature_exc:
            try:
                _exchange_paths(app, staged_app)
            except Exception as rollback_exc:
                raise _BundleRollbackError(
                    f"{signature_exc}; rollback failed: {rollback_exc}"
                ) from rollback_exc
            raise _PreserveStagingError(
                f"{signature_exc}; the prior bundle was restored and the failed "
                "replacement was preserved"
            ) from signature_exc
        shutil.rmtree(staged_app, ignore_errors=True)
        raise PatchError(f"Codex Desktop changed after {operation}; retry")

    try:
        displaced_identity = _bundle_identity(staged_app)
    except Exception as exc:
        raise _PreserveStagingError(
            f"Could not verify displaced Codex Desktop bundle after {operation}; "
            "the verified bundle remains installed and the failed replacement "
            "was preserved"
        ) from exc
    if displaced_identity != expected_live:
        try:
            _verify_openai_signed_bundle(
                staged_app,
                command_runner,
                "Displaced application failed OpenAI signature verification",
            )
        except PatchError as signature_exc:
            raise _PreserveStagingError(
                f"{signature_exc}; the verified bundle remains installed and the "
                "failed replacement was preserved"
            ) from signature_exc
        try:
            _exchange_paths(app, staged_app)
        except Exception as rollback_exc:
            raise _BundleRollbackError(
                f"Codex Desktop changed during {operation}; "
                f"rollback failed: {rollback_exc}"
            ) from rollback_exc
        shutil.rmtree(staged_app, ignore_errors=True)
        raise PatchError(
            f"Codex Desktop changed during {operation}; retry against the updated app"
        )
    shutil.rmtree(staged_app, ignore_errors=True)


def _restore_full_bundle(
    app: Path, backup: Path, version: str, build: str, command_runner
) -> None:
    _validate_full_backup(backup, version, build)
    live_identity = _bundle_identity(app)
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{app.name}.restore-", dir=app.parent)
    )
    staged_app = staging_root / app.name
    preserve_staging = False
    try:
        shutil.copytree(
            backup, staged_app, symlinks=True, copy_function=shutil.copy2
        )
        _verify_full_backup(staged_app, version, build, command_runner)
        staged_identity = _bundle_identity(staged_app)
        if _bundle_identity(app) != live_identity:
            raise PatchError("Codex Desktop changed during restoration")
        try:
            _activate_staged_bundle(
                app,
                staged_app,
                live_identity,
                staged_identity,
                "restoration",
                command_runner,
            )
        except _PreserveStagingError:
            preserve_staging = True
            raise
    finally:
        if app.exists() and not preserve_staging:
            shutil.rmtree(staging_root, ignore_errors=True)


def _run_checked(command_runner, command: list[str], failure_prefix: str) -> None:
    try:
        completed = command_runner(command, capture_output=True, text=True)
    except OSError as exc:
        raise PatchError(f"{failure_prefix}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or failure_prefix).strip()
        raise PatchError(detail)


def _verify_openai_signed_bundle(
    target: Path, command_runner, failure_prefix: str
) -> None:
    _run_checked(
        command_runner,
        [
            "codesign",
            "--verify",
            "--deep",
            "--strict",
            f"-R={_OPENAI_DESIGNATED_REQUIREMENT}",
            str(target),
        ],
        failure_prefix,
    )


def _read_desktop_metadata(app: Path) -> tuple[str, str]:
    info_path = app / "Contents" / "Info.plist"
    archive = app / "Contents" / "Resources" / "app.asar"
    if not info_path.is_file() or not archive.is_file():
        raise PatchError(f"Not a Codex Desktop application bundle: {app}")
    try:
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise PatchError(f"Invalid application Info.plist: {info_path}") from exc
    if info.get("CFBundleIdentifier") != _CODEX_BUNDLE_ID:
        raise PatchError(
            f"Unexpected bundle identifier: {info.get('CFBundleIdentifier')!r}"
        )
    version = str(info.get("CFBundleShortVersionString") or "unknown")
    build = str(info.get("CFBundleVersion") or "unknown")
    if not all(_SAFE_VERSION_COMPONENT.fullmatch(item) for item in (version, build)):
        raise PatchError("Unsafe Codex Desktop version metadata")
    return version, build


def _bundle_identity(app: Path) -> _BundleIdentity:
    version, build = _read_desktop_metadata(app)
    archive = app / "Contents" / "Resources" / "app.asar"
    bundle_stat = app.stat()
    archive_stat = archive.stat()
    return _BundleIdentity(
        version=version,
        build=build,
        bundle_device=bundle_stat.st_dev,
        bundle_inode=bundle_stat.st_ino,
        archive_device=archive_stat.st_dev,
        archive_inode=archive_stat.st_ino,
        archive_size=archive_stat.st_size,
        archive_hash=_sha256(archive),
    )


def _verify_full_backup(
    backup: Path, version: str, build: str, command_runner
) -> None:
    _validate_full_backup(backup, version, build)
    _verify_openai_signed_bundle(
        backup,
        command_runner,
        "Original application backup failed signature verification",
    )


def _find_full_backup(
    backups: Path, version: str, build: str, command_runner
) -> Path:
    candidates = (
        backups / f"ChatGPT-{version}-{build}-original.app",
        backups / f"ChatGPT-{version}-original.app",
    )
    failures: list[str] = []
    for candidate in candidates:
        if candidate.exists():
            try:
                _verify_full_backup(candidate, version, build, command_runner)
            except (OSError, PatchError) as exc:
                failures.append(f"{candidate}: {exc}")
                continue
            else:
                return candidate
    if failures:
        raise PatchError("No valid full application backup: " + "; ".join(failures))
    raise PatchError(
        f"No full original application backup exists for Codex Desktop {version} ({build})"
    )


def _installer_lock_path(app: Path) -> Path:
    identity = hashlib.sha256(
        str(app.resolve(strict=False)).encode("utf-8")
    ).hexdigest()
    lock_dir = Path(tempfile.gettempdir()) / "backdoor-codex-desktop-locks"
    return lock_dir / f"{identity}.lock"


@contextmanager
def _installer_lock(app: Path):
    lock_path = _installer_lock_path(app)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def patch_desktop_app(app_path, backup_root, runner=None):
    app = Path(app_path).resolve(strict=False)
    backups = Path(backup_root).resolve(strict=False)
    if backups == app or app in backups.parents:
        raise PatchError("Backup root cannot be inside the application bundle")
    command_runner = runner or subprocess.run

    with _installer_lock(app):
        version, build = _read_desktop_metadata(app)
        archive = app / "Contents" / "Resources" / "app.asar"
        return _patch_desktop_app_locked(
            app, backups, archive, version, build, command_runner
        )


def _patch_desktop_app_locked(
    app: Path,
    backups: Path,
    archive: Path,
    version: str,
    build: str,
    command_runner,
):
    backups.mkdir(parents=True, exist_ok=True)
    state = _archive_state(archive)
    live_identity = _bundle_identity(app)

    archive_backups = backups / "archives" / version / build
    if state == "patched":
        try:
            _run_checked(
                command_runner,
                ["codesign", "--verify", "--deep", "--strict", str(app)],
                "Patched application failed signature verification",
            )
        except PatchError as exc:
            full_backup = _find_full_backup(
                backups, version, build, command_runner
            )
            _restore_full_bundle(
                app, full_backup, version, build, command_runner
            )
            raise PatchError(f"{exc}; restored the original application") from exc

        if (
            _read_desktop_metadata(app) != (version, build)
            or _archive_state(archive) != "patched"
        ):
            raise PatchError("Codex Desktop changed during installation")
        patched_hash = _sha256(archive)
        try:
            backup_path = _recorded_archive_backup(archive_backups, patched_hash)
        except PatchError as exc:
            full_backup = _find_full_backup(
                backups, version, build, command_runner
            )
            _restore_full_bundle(
                app, full_backup, version, build, command_runner
            )
            raise PatchError(f"{exc}; restored the original application") from exc
        if (
            _read_desktop_metadata(app) != (version, build)
            or _archive_state(archive) != "patched"
            or _sha256(archive) != patched_hash
        ):
            raise PatchError("Codex Desktop changed during installation")
        return PatchResult(changed=False, backup_path=backup_path)

    if state == "legacy-patched":
        full_backup = _find_full_backup(backups, version, build, command_runner)
    else:
        full_backup = backups / f"ChatGPT-{version}-{build}-original.app"
    if state == "unpatched" and not full_backup.exists():
        with tempfile.TemporaryDirectory(
            prefix=".codex-desktop-backup-", dir=backups
        ) as temporary:
            staged_backup = Path(temporary) / full_backup.name
            shutil.copytree(
                app, staged_backup, symlinks=True, copy_function=shutil.copy2
            )
            _verify_full_backup(
                staged_backup, version, build, command_runner
            )
            if _read_desktop_metadata(app) != (version, build):
                raise PatchError("Codex Desktop changed during installation")
            staged_backup.replace(full_backup)
    else:
        _verify_full_backup(full_backup, version, build, command_runner)

    if _bundle_identity(app) != live_identity:
        raise PatchError("Codex Desktop changed during installation")
    if state == "unpatched" and live_identity.archive_hash != _sha256(
        full_backup / "Contents" / "Resources" / "app.asar"
    ):
        raise PatchError("Codex Desktop changed during installation")

    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{app.name}.patch-", dir=app.parent)
    )
    staged_app = staging_root / app.name
    preserve_staging = False
    try:
        shutil.copytree(
            app, staged_app, symlinks=True, copy_function=shutil.copy2
        )
        if _bundle_identity(app) != live_identity:
            raise PatchError("Codex Desktop changed during installation")
        staged_archive = staged_app / "Contents" / "Resources" / "app.asar"
        result = apply_archive_patch(
            staged_archive,
            archive_backups,
            original_backup_path=(
                full_backup / "Contents" / "Resources" / "app.asar"
                if state == "legacy-patched"
                else None
            ),
        )
        staged_sign_command = [
            "codesign",
            "--force",
            "--sign",
            "-",
            "--preserve-metadata=entitlements,requirements,flags,runtime",
            str(staged_app),
        ]
        staged_verify_command = [
            "codesign",
            "--verify",
            "--deep",
            "--strict",
            str(staged_app),
        ]
        _run_checked(command_runner, staged_sign_command, "codesign failed")
        _run_checked(
            command_runner,
            staged_verify_command,
            "codesign verification failed",
        )
        staged_identity = _bundle_identity(staged_app)
        if _bundle_identity(app) != live_identity:
            raise PatchError("Codex Desktop changed during installation")

        try:
            _activate_staged_bundle(
                app,
                staged_app,
                live_identity,
                staged_identity,
                "installation",
                command_runner,
            )
        except PatchError as exc:
            if isinstance(exc, _PreserveStagingError):
                preserve_staging = True
            raise
        return result
    except PatchError:
        raise
    except Exception as exc:
        raise PatchError(str(exc)) from exc
    finally:
        if not preserve_staging:
            shutil.rmtree(staging_root, ignore_errors=True)


def restore_desktop_app(app_path, backup_root, runner=None) -> Path:
    app = Path(app_path).resolve(strict=False)
    backups = Path(backup_root)
    command_runner = runner or subprocess.run
    with _installer_lock(app):
        version, build = _read_desktop_metadata(app)
        full_backup = _find_full_backup(backups, version, build, command_runner)
        _restore_full_bundle(
            app, full_backup, version, build, command_runner
        )
        return full_backup
