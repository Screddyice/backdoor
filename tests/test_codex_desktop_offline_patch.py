import ctypes
import errno
import hashlib
import multiprocessing
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

import pytest

import src.proxy.codex_desktop_offline_patch as patch_module
from src.proxy.codex_desktop_offline_patch import (
    ORIGINAL_AUTH_EXPRESSION,
    PATCHED_AUTH_EXPRESSION,
    PatchError,
    apply_archive_patch,
    patch_desktop_app,
    restore_archive,
)


def test_patch_preserves_true_token_and_maps_transient_false_to_unknown(tmp_path):
    archive = tmp_path / "app.asar"
    archive.write_bytes(b"prefix:" + ORIGINAL_AUTH_EXPRESSION + b":suffix")
    backup_dir = tmp_path / "backups"

    result = apply_archive_patch(archive, backup_dir)

    assert archive.read_bytes() == b"prefix:" + PATCHED_AUTH_EXPRESSION + b":suffix"
    assert result.changed is True
    assert result.backup_path is not None
    assert result.backup_path.read_bytes() == (
        b"prefix:" + ORIGINAL_AUTH_EXPRESSION + b":suffix"
    )
    assert len(PATCHED_AUTH_EXPRESSION) == len(ORIGINAL_AUTH_EXPRESSION)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_patched_javascript_preserves_non_chatgpt_unknown_state():
    expression = PATCHED_AUTH_EXPRESSION.decode().split("=", 1)[1]
    script = """
const evaluate = Function("y", "h", `return (${process.argv[1]})`);
const values = [
  evaluate(false, {hasChatGptToken: true}),
  evaluate(true, {hasChatGptToken: false}),
  evaluate(true, {hasChatGptToken: true}),
  evaluate(true, undefined),
].map((value) => value === undefined ? "undefined" : value);
process.stdout.write(JSON.stringify(values));
"""

    completed = subprocess.run(
        ["node", "-e", script, expression], capture_output=True, text=True
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == '["undefined","undefined",true,"undefined"]'


def test_patch_is_idempotent_and_does_not_replace_its_backup(tmp_path):
    archive = tmp_path / "app.asar"
    archive.write_bytes(ORIGINAL_AUTH_EXPRESSION)
    backup_dir = tmp_path / "backups"
    first = apply_archive_patch(archive, backup_dir)

    second = apply_archive_patch(archive, backup_dir)

    assert second.changed is False
    assert second.backup_path == first.backup_path
    assert second.backup_path is not None
    assert second.backup_path.read_bytes() == ORIGINAL_AUTH_EXPRESSION


@pytest.mark.parametrize(
    "payload",
    [b"", b"no supported expression", ORIGINAL_AUTH_EXPRESSION * 2],
)
def test_patch_refuses_unknown_or_ambiguous_desktop_bundles(tmp_path, payload):
    archive = tmp_path / "app.asar"
    archive.write_bytes(payload)

    with pytest.raises(PatchError):
        apply_archive_patch(archive, tmp_path / "backups")

    assert archive.read_bytes() == payload


def test_patch_refuses_a_corrupt_existing_archive_backup(tmp_path):
    archive = tmp_path / "app.asar"
    archive.write_bytes(ORIGINAL_AUTH_EXPRESSION)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    original_hash = hashlib.sha256(ORIGINAL_AUTH_EXPRESSION).hexdigest()
    corrupt_backup = backup_dir / f"app.asar.{original_hash}.backup"
    corrupt_backup.write_bytes(b"corrupt")

    with pytest.raises(PatchError, match="backup"):
        apply_archive_patch(archive, backup_dir)

    assert archive.read_bytes() == ORIGINAL_AUTH_EXPRESSION


def test_interrupted_archive_backup_copy_never_publishes_partial_backup(
    tmp_path, monkeypatch
):
    archive = tmp_path / "app.asar"
    archive.write_bytes(ORIGINAL_AUTH_EXPRESSION)
    backup_dir = tmp_path / "backups"

    def interrupted_copy(source, destination, *args, **kwargs):
        Path(destination).write_bytes(b"partial")
        raise OSError("copy interrupted")

    monkeypatch.setattr(shutil, "copy2", interrupted_copy)

    with pytest.raises(OSError, match="copy interrupted"):
        apply_archive_patch(archive, backup_dir)

    assert not list(backup_dir.glob("*.backup"))
    assert archive.read_bytes() == ORIGINAL_AUTH_EXPRESSION


def test_idempotent_patch_rejects_a_corrupt_recorded_archive_backup(tmp_path):
    archive = tmp_path / "app.asar"
    archive.write_bytes(ORIGINAL_AUTH_EXPRESSION)
    backup_dir = tmp_path / "backups"
    result = apply_archive_patch(archive, backup_dir)
    result.backup_path.write_bytes(b"corrupt")

    with pytest.raises(PatchError, match="backup"):
        apply_archive_patch(archive, backup_dir)


def test_idempotent_patch_rejects_manifest_path_traversal(tmp_path):
    archive = tmp_path / "app.asar"
    archive.write_bytes(PATCHED_AUTH_EXPRESSION)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    patched_hash = hashlib.sha256(PATCHED_AUTH_EXPRESSION).hexdigest()
    (backup_dir / "manifest.json").write_text(
        '{"' + patched_hash + '": "../../outside.backup"}\n'
    )

    with pytest.raises(PatchError, match="manifest backup path"):
        apply_archive_patch(archive, backup_dir)


@pytest.mark.parametrize(
    "manifest_payload",
    ["{not-json\n", "[]\n", '{"patched-hash": 123}\n'],
)
def test_idempotent_patch_rejects_malformed_or_wrong_type_manifest(
    tmp_path, manifest_payload
):
    archive = tmp_path / "app.asar"
    archive.write_bytes(PATCHED_AUTH_EXPRESSION)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "manifest.json").write_text(manifest_payload)

    with pytest.raises(PatchError, match="Invalid backup manifest"):
        apply_archive_patch(archive, backup_dir)

    assert archive.read_bytes() == PATCHED_AUTH_EXPRESSION


def test_legacy_patch_requires_an_original_backup(tmp_path):
    archive = tmp_path / "app.asar"
    archive.write_bytes(patch_module.LEGACY_PATCHED_AUTH_EXPRESSION)

    with pytest.raises(PatchError, match="requires an original backup"):
        apply_archive_patch(archive, tmp_path / "backups")

    assert archive.read_bytes() == patch_module.LEGACY_PATCHED_AUTH_EXPRESSION


def test_legacy_patch_rejects_a_non_original_backup(tmp_path):
    archive = tmp_path / "app.asar"
    archive.write_bytes(patch_module.LEGACY_PATCHED_AUTH_EXPRESSION)
    invalid_original = tmp_path / "invalid-original.asar"
    invalid_original.write_bytes(PATCHED_AUTH_EXPRESSION)

    with pytest.raises(PatchError, match="backup is not original"):
        apply_archive_patch(
            archive,
            tmp_path / "backups",
            original_backup_path=invalid_original,
        )

    assert archive.read_bytes() == patch_module.LEGACY_PATCHED_AUTH_EXPRESSION


def test_restore_recovers_the_exact_signed_archive_bytes(tmp_path):
    archive = tmp_path / "app.asar"
    original = b"prefix:" + ORIGINAL_AUTH_EXPRESSION + b":suffix"
    archive.write_bytes(original)
    result = apply_archive_patch(archive, tmp_path / "backups")

    restore_archive(archive, result.backup_path)

    assert archive.read_bytes() == original


def test_restore_archive_rejects_an_unverified_backup(tmp_path):
    archive = tmp_path / "app.asar"
    archive.write_bytes(PATCHED_AUTH_EXPRESSION)
    wrong_backup = tmp_path / "wrong.backup"
    wrong_backup.write_bytes(ORIGINAL_AUTH_EXPRESSION)

    with pytest.raises(PatchError, match="content-addressed"):
        restore_archive(archive, wrong_backup)

    assert archive.read_bytes() == PATCHED_AUTH_EXPRESSION


def _fake_app(tmp_path: Path, bundle_id: str = "com.openai.codex") -> Path:
    app = tmp_path / "ChatGPT.app"
    resources = app / "Contents" / "Resources"
    resources.mkdir(parents=True)
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": bundle_id,
                "CFBundleShortVersionString": "26.825.32147",
                "CFBundleVersion": "7303",
            },
            handle,
        )
    (resources / "app.asar").write_bytes(ORIGINAL_AUTH_EXPRESSION)
    return app


def _acquire_installer_lock_in_child(app_path, started, acquired):
    started.set()
    with patch_module._installer_lock(Path(app_path)):
        acquired.set()


def _is_patch_staged_app(path) -> bool:
    target = Path(path)
    return (
        target.name == "ChatGPT.app"
        and target.parent.name.startswith(".ChatGPT.app.patch-")
    )


def _prepare_bundle_operation(tmp_path: Path, operation: str):
    app = _fake_app(tmp_path)
    backups = tmp_path / "backups"
    archive = app / "Contents" / "Resources" / "app.asar"
    if operation == "restoration":
        full_backup = backups / "ChatGPT-26.825.32147-7303-original.app"
        full_backup.parent.mkdir()
        shutil.copytree(app, full_backup)
        archive.write_bytes(PATCHED_AUTH_EXPRESSION)
    return app, backups, archive.read_bytes()


def _run_bundle_operation(operation, app, backups, runner):
    if operation == "installation":
        return patch_desktop_app(app, backups, runner=runner)
    return patch_module.restore_desktop_app(app, backups, runner=runner)


def _preserved_staged_apps(tmp_path: Path, operation: str) -> list[Path]:
    stage = "patch" if operation == "installation" else "restore"
    return [
        root / "ChatGPT.app"
        for root in tmp_path.glob(f".ChatGPT.app.{stage}-*")
        if (root / "ChatGPT.app").exists()
    ]


def test_desktop_install_keeps_a_full_recoverable_app_backup(tmp_path):
    app = _fake_app(tmp_path)
    commands: list[list[str]] = []

    def successful_signer(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    result = patch_desktop_app(app, tmp_path / "backups", runner=successful_signer)

    backup_app = (
        tmp_path / "backups" / "ChatGPT-26.825.32147-7303-original.app"
    )
    assert result.changed is True
    assert backup_app.is_dir()
    assert (backup_app / "Contents" / "Resources" / "app.asar").read_bytes() == (
        ORIGINAL_AUTH_EXPRESSION
    )
    assert (app / "Contents" / "Resources" / "app.asar").read_bytes() == (
        PATCHED_AUTH_EXPRESSION
    )
    assert commands[0][:3] == ["codesign", "--verify", "--deep"]
    requirement_argument = next(
        item for item in commands[0] if item.startswith("-R=")
    )
    assert "2DC432GLL2" in requirement_argument
    assert commands[1][:4] == ["codesign", "--force", "--sign", "-"]
    assert commands[2][:3] == ["codesign", "--verify", "--deep"]


def test_desktop_install_restores_the_original_app_when_signing_fails(tmp_path):
    app = _fake_app(tmp_path)

    def failed_signer(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "signing failed")

    with pytest.raises(PatchError, match="signing failed"):
        patch_desktop_app(app, tmp_path / "backups", runner=failed_signer)

    assert (app / "Contents" / "Resources" / "app.asar").read_bytes() == (
        ORIGINAL_AUTH_EXPRESSION
    )


def test_desktop_install_restores_the_original_app_when_codesign_raises(tmp_path):
    app = _fake_app(tmp_path)

    def unavailable_signer(command, **kwargs):
        if "--force" in command:
            raise OSError("codesign unavailable")
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(PatchError, match="codesign unavailable"):
        patch_desktop_app(app, tmp_path / "backups", runner=unavailable_signer)

    assert (app / "Contents" / "Resources" / "app.asar").read_bytes() == (
        ORIGINAL_AUTH_EXPRESSION
    )


def test_desktop_install_restores_the_original_app_when_manifest_write_fails(
    tmp_path, monkeypatch
):
    app = _fake_app(tmp_path)

    def failed_manifest_write(*args):
        raise OSError("manifest disk full")

    monkeypatch.setattr(patch_module, "_write_manifest", failed_manifest_write)

    with pytest.raises(PatchError, match="manifest disk full"):
        patch_desktop_app(
            app,
            tmp_path / "backups",
            runner=lambda command, **kwargs: subprocess.CompletedProcess(
                command, 0, "", ""
            ),
        )

    assert (app / "Contents" / "Resources" / "app.asar").read_bytes() == (
        ORIGINAL_AUTH_EXPRESSION
    )


def test_desktop_install_refuses_an_incomplete_existing_full_backup(tmp_path):
    app = _fake_app(tmp_path)
    backups = tmp_path / "backups"
    incomplete_backup = backups / "ChatGPT-26.825.32147-7303-original.app"
    incomplete_backup.mkdir(parents=True)

    with pytest.raises(PatchError, match="backup"):
        patch_desktop_app(
            app,
            backups,
            runner=lambda command, **kwargs: subprocess.CompletedProcess(
                command, 0, "", ""
            ),
        )

    assert (app / "Contents" / "Resources" / "app.asar").read_bytes() == (
        ORIGINAL_AUTH_EXPRESSION
    )


def test_failed_staged_backup_verification_never_publishes_the_backup(tmp_path):
    app = _fake_app(tmp_path)
    backups = tmp_path / "backups"

    def signature_runner(command, **kwargs):
        target = command[-1]
        returncode = 1 if ".codex-desktop-backup-" in target else 0
        return subprocess.CompletedProcess(command, returncode, "", "invalid signature")

    with pytest.raises(PatchError, match="invalid signature"):
        patch_desktop_app(app, backups, runner=signature_runner)

    assert not (
        backups / "ChatGPT-26.825.32147-7303-original.app"
    ).exists()
    assert (app / "Contents" / "Resources" / "app.asar").read_bytes() == (
        ORIGINAL_AUTH_EXPRESSION
    )


def test_restore_verifies_staged_copy_before_exchanging_the_live_app(tmp_path):
    app = _fake_app(tmp_path)
    backups = tmp_path / "backups"
    backup = backups / "ChatGPT-26.825.32147-7303-original.app"
    backup.parent.mkdir()
    shutil.copytree(app, backup)
    (app / "Contents" / "Resources" / "app.asar").write_bytes(
        PATCHED_AUTH_EXPRESSION
    )

    def signature_runner(command, **kwargs):
        target = command[-1]
        returncode = 1 if ".restore-" in target else 0
        return subprocess.CompletedProcess(command, returncode, "", "staged corruption")

    with pytest.raises(PatchError, match="staged corruption"):
        patch_module.restore_desktop_app(app, backups, runner=signature_runner)

    assert (app / "Contents" / "Resources" / "app.asar").read_bytes() == (
        PATCHED_AUTH_EXPRESSION
    )


def test_full_backup_lookup_falls_back_to_a_valid_legacy_backup(tmp_path):
    app = _fake_app(tmp_path)
    backups = tmp_path / "backups"
    invalid_exact = backups / "ChatGPT-26.825.32147-7303-original.app"
    invalid_exact.mkdir(parents=True)
    legacy_backup = backups / "ChatGPT-26.825.32147-original.app"
    shutil.copytree(app, legacy_backup)
    (app / "Contents" / "Resources" / "app.asar").write_bytes(
        PATCHED_AUTH_EXPRESSION
    )

    restored = patch_module.restore_desktop_app(
        app,
        backups,
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "", ""
        ),
    )

    assert restored == legacy_backup


def test_full_backup_lookup_reports_when_no_backup_exists(tmp_path):
    app = _fake_app(tmp_path)
    archive = app / "Contents" / "Resources" / "app.asar"
    archive.write_bytes(PATCHED_AUTH_EXPRESSION)

    with pytest.raises(
        PatchError, match="No full original application backup exists"
    ):
        patch_module.restore_desktop_app(
            app,
            tmp_path / "empty-backups",
            runner=lambda command, **kwargs: subprocess.CompletedProcess(
                command, 0, "", ""
            ),
        )

    assert archive.read_bytes() == PATCHED_AUTH_EXPRESSION


def test_full_backup_lookup_reports_all_invalid_candidates(tmp_path):
    app = _fake_app(tmp_path)
    archive = app / "Contents" / "Resources" / "app.asar"
    archive.write_bytes(PATCHED_AUTH_EXPRESSION)
    backups = tmp_path / "backups"
    exact = backups / "ChatGPT-26.825.32147-7303-original.app"
    legacy = backups / "ChatGPT-26.825.32147-original.app"
    exact.mkdir(parents=True)
    legacy.mkdir()

    with pytest.raises(PatchError, match="No valid full application backup") as error:
        patch_module.restore_desktop_app(
            app,
            backups,
            runner=lambda command, **kwargs: subprocess.CompletedProcess(
                command, 0, "", ""
            ),
        )

    assert str(exact) in str(error.value)
    assert str(legacy) in str(error.value)
    assert archive.read_bytes() == PATCHED_AUTH_EXPRESSION


def test_installer_lock_identity_does_not_depend_on_backup_root(tmp_path):
    app = _fake_app(tmp_path)

    first = patch_module._installer_lock_path(app)
    second = patch_module._installer_lock_path(app)

    assert first == second
    assert str(tmp_path / "backups-a") not in str(first)
    assert str(tmp_path / "backups-b") not in str(second)


def test_installer_lock_serializes_processes_and_releases_after_exception(tmp_path):
    app = _fake_app(tmp_path)
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    acquired = context.Event()
    process = context.Process(
        target=_acquire_installer_lock_in_child,
        args=(str(app), started, acquired),
    )

    try:
        with pytest.raises(RuntimeError, match="release installer lock"):
            with patch_module._installer_lock(app):
                process.start()
                assert started.wait(5)
                assert not acquired.wait(0.25)
                raise RuntimeError("release installer lock")

        assert acquired.wait(5)
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS atomic exchange")
def test_atomic_exchange_swaps_complete_application_directories(tmp_path):
    installed = tmp_path / "installed.app"
    staged = tmp_path / "staged.app"
    installed.mkdir()
    staged.mkdir()
    (installed / "state").write_text("patched")
    (staged / "state").write_text("original")

    patch_module._exchange_paths(installed, staged)

    assert (installed / "state").read_text() == "original"
    assert (staged / "state").read_text() == "patched"


def test_darwin_atomic_exchange_propagates_errno(tmp_path, monkeypatch):
    left = tmp_path / "left.app"
    right = tmp_path / "right.app"
    left.mkdir()
    right.mkdir()
    (left / "state").write_text("left")
    (right / "state").write_text("right")

    class FailingRename:
        argtypes = None
        restype = None

        def __call__(self, *args):
            ctypes.set_errno(errno.EBUSY)
            return -1

    class FakeLibc:
        renameatx_np = FailingRename()

    monkeypatch.setattr(patch_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        patch_module.ctypes, "CDLL", lambda *args, **kwargs: FakeLibc()
    )

    with pytest.raises(OSError) as error:
        patch_module._exchange_paths(left, right)

    assert error.value.errno == errno.EBUSY
    assert (left / "state").read_text() == "left"
    assert (right / "state").read_text() == "right"


def test_fallback_exchange_restores_the_left_path_when_second_rename_fails(
    tmp_path, monkeypatch
):
    installed = tmp_path / "installed.app"
    staged = tmp_path / "staged.app"
    installed.mkdir()
    staged.mkdir()
    (installed / "state").write_text("patched")
    (staged / "state").write_text("original")
    original_replace = Path.replace

    def fail_second_rename(source, destination):
        if source == staged and Path(destination) == installed:
            raise OSError("exchange interrupted")
        return original_replace(source, destination)

    monkeypatch.setattr(patch_module.sys, "platform", "linux")
    monkeypatch.setattr(Path, "replace", fail_second_rename)

    with pytest.raises(OSError, match="exchange interrupted"):
        patch_module._exchange_paths(installed, staged)

    assert (installed / "state").read_text() == "patched"
    assert (staged / "state").read_text() == "original"
    assert not (tmp_path / ".installed.app.exchange").exists()


def test_fallback_exchange_restores_both_paths_when_final_rename_fails(
    tmp_path, monkeypatch
):
    installed = tmp_path / "installed.app"
    staged = tmp_path / "staged.app"
    exchange = tmp_path / ".installed.app.exchange"
    installed.mkdir()
    staged.mkdir()
    (installed / "state").write_text("patched")
    (staged / "state").write_text("original")
    original_replace = Path.replace

    def fail_final_rename(source, destination):
        if source == exchange and Path(destination) == staged:
            raise OSError("final exchange rename interrupted")
        return original_replace(source, destination)

    monkeypatch.setattr(patch_module.sys, "platform", "linux")
    monkeypatch.setattr(Path, "replace", fail_final_rename)

    with pytest.raises(OSError, match="final exchange rename interrupted"):
        patch_module._exchange_paths(installed, staged)

    assert (installed / "state").read_text() == "patched"
    assert (staged / "state").read_text() == "original"
    assert not exchange.exists()


def test_desktop_install_refuses_a_different_application_bundle(tmp_path):
    app = _fake_app(tmp_path, bundle_id="com.example.not-codex")

    with pytest.raises(PatchError, match="bundle identifier"):
        patch_desktop_app(app, tmp_path / "backups")

    assert (app / "Contents" / "Resources" / "app.asar").read_bytes() == (
        ORIGINAL_AUTH_EXPRESSION
    )


def test_desktop_install_rejects_unsafe_version_path_components(tmp_path):
    app = _fake_app(tmp_path)
    info_path = app / "Contents" / "Info.plist"
    with info_path.open("rb") as handle:
        info = plistlib.load(handle)
    info["CFBundleVersion"] = "../../escape"
    with info_path.open("wb") as handle:
        plistlib.dump(info, handle)

    with pytest.raises(PatchError, match="version metadata"):
        patch_desktop_app(app, tmp_path / "backups")

    assert not (tmp_path / "escape").exists()


def test_desktop_install_rejects_a_backup_root_inside_the_app(tmp_path):
    app = _fake_app(tmp_path)
    nested_backup = app / "backups"

    with pytest.raises(PatchError, match="inside the application"):
        patch_desktop_app(app, nested_backup)

    assert not nested_backup.exists()


def test_desktop_install_detects_an_app_update_before_mutation(tmp_path):
    app = _fake_app(tmp_path)

    def updating_signature_runner(command, **kwargs):
        if ".codex-desktop-backup-" in command[-1]:
            info_path = app / "Contents" / "Info.plist"
            with info_path.open("rb") as handle:
                info = plistlib.load(handle)
            info["CFBundleVersion"] = "7304"
            with info_path.open("wb") as handle:
                plistlib.dump(info, handle)
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(PatchError, match="changed during installation"):
        patch_desktop_app(
            app, tmp_path / "backups", runner=updating_signature_runner
        )

    assert (app / "Contents" / "Resources" / "app.asar").read_bytes() == (
        ORIGINAL_AUTH_EXPRESSION
    )


def test_desktop_install_detects_an_asar_only_update_before_mutation(tmp_path):
    app = _fake_app(tmp_path)
    archive = app / "Contents" / "Resources" / "app.asar"
    updated_archive = b"updated:" + ORIGINAL_AUTH_EXPRESSION

    def updating_signature_runner(command, **kwargs):
        if ".codex-desktop-backup-" in command[-1]:
            archive.write_bytes(updated_archive)
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(PatchError, match="changed during installation"):
        patch_desktop_app(
            app, tmp_path / "backups", runner=updating_signature_runner
        )

    assert archive.read_bytes() == updated_archive
    assert PATCHED_AUTH_EXPRESSION not in archive.read_bytes()


def test_desktop_install_rolls_back_when_codesign_returns_nonzero(tmp_path):
    app = _fake_app(tmp_path)

    def failed_signer(command, **kwargs):
        if "--force" in command:
            return subprocess.CompletedProcess(command, 1, "", "signing failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(PatchError, match="signing failed"):
        patch_desktop_app(app, tmp_path / "backups", runner=failed_signer)

    assert (app / "Contents" / "Resources" / "app.asar").read_bytes() == (
        ORIGINAL_AUTH_EXPRESSION
    )


def test_desktop_install_rolls_back_when_final_verification_returns_nonzero(
    tmp_path,
):
    app = _fake_app(tmp_path)

    def failed_verifier(command, **kwargs):
        is_final_verification = (
            "--verify" in command
            and not any(item.startswith("-R=") for item in command)
            and _is_patch_staged_app(command[-1])
        )
        if is_final_verification:
            return subprocess.CompletedProcess(
                command, 1, "", "verification failed"
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(PatchError, match="verification failed"):
        patch_desktop_app(app, tmp_path / "backups", runner=failed_verifier)

    assert (app / "Contents" / "Resources" / "app.asar").read_bytes() == (
        ORIGINAL_AUTH_EXPRESSION
    )


def test_desktop_install_leaves_live_update_untouched_before_exchange(
    tmp_path, monkeypatch
):
    app = _fake_app(tmp_path)
    archive = app / "Contents" / "Resources" / "app.asar"
    updated_archive = b"updater:" + ORIGINAL_AUTH_EXPRESSION

    def updater_runner(command, **kwargs):
        is_staged_verification = (
            "--verify" in command
            and not any(item.startswith("-R=") for item in command)
            and _is_patch_staged_app(command[-1])
        )
        if is_staged_verification:
            archive.write_bytes(updated_archive)
        return subprocess.CompletedProcess(command, 0, "", "")

    def unexpected_exchange(*args):
        raise AssertionError("exchange should not run after the live update")

    monkeypatch.setattr(patch_module, "_exchange_paths", unexpected_exchange)

    with pytest.raises(PatchError, match="changed during installation"):
        patch_desktop_app(app, tmp_path / "backups", runner=updater_runner)

    assert archive.read_bytes() == updated_archive


@pytest.mark.parametrize("operation", ["installation", "restoration"])
def test_bundle_operation_accepts_signed_updater_displaced_during_exchange(
    tmp_path, monkeypatch, operation
):
    app, backups, _ = _prepare_bundle_operation(tmp_path, operation)
    archive = app / "Contents" / "Resources" / "app.asar"
    updated_archive = b"updater:" + ORIGINAL_AUTH_EXPRESSION
    original_exchange = patch_module._exchange_paths
    exchanged_paths: list[tuple[Path, Path]] = []
    commands: list[list[str]] = []

    def updater_wins_exchange(left, right):
        exchanged_paths.append((left, right))
        if len(exchanged_paths) == 1:
            archive.write_bytes(updated_archive)
        original_exchange(left, right)

    def updater_signature_runner(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(patch_module, "_exchange_paths", updater_wins_exchange)

    with pytest.raises(PatchError, match=f"changed during {operation}"):
        _run_bundle_operation(
            operation,
            app,
            backups,
            runner=updater_signature_runner,
        )

    assert len(exchanged_paths) == 2
    assert all(left == app for left, _ in exchanged_paths)
    assert archive.read_bytes() == updated_archive
    assert _preserved_staged_apps(tmp_path, operation) == []
    stage = "patch" if operation == "installation" else "restore"
    assert any(
        Path(command[-1]).parent.name.startswith(f".ChatGPT.app.{stage}-")
        and any(item.startswith("-R=") for item in command)
        for command in commands
    )


@pytest.mark.parametrize("failure_point", ["copy", "sign", "verify"])
def test_desktop_install_staging_failures_never_mutate_live_bundle(
    tmp_path, monkeypatch, failure_point
):
    app = _fake_app(tmp_path)
    original_identity = patch_module._bundle_identity(app)
    original_copytree = shutil.copytree

    def staging_copy(source, destination, *args, **kwargs):
        if _is_patch_staged_app(destination) and failure_point == "copy":
            raise OSError("staging copy failed")
        return original_copytree(source, destination, *args, **kwargs)

    def staging_runner(command, **kwargs):
        is_staged_command = _is_patch_staged_app(command[-1])
        if failure_point == "sign" and is_staged_command and "--force" in command:
            return subprocess.CompletedProcess(command, 1, "", "staging sign failed")
        if (
            failure_point == "verify"
            and is_staged_command
            and "--verify" in command
        ):
            return subprocess.CompletedProcess(
                command, 1, "", "staging verify failed"
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(patch_module.shutil, "copytree", staging_copy)

    with pytest.raises(PatchError, match=f"staging {failure_point} failed"):
        patch_desktop_app(
            app, tmp_path / "backups", runner=staging_runner
        )

    assert patch_module._bundle_identity(app) == original_identity


def test_desktop_install_exchanges_the_verified_staged_application(
    tmp_path, monkeypatch
):
    app = _fake_app(tmp_path)
    archive = app / "Contents" / "Resources" / "app.asar"
    original_identity = patch_module._bundle_identity(app)
    original_exchange = patch_module._exchange_paths
    exchanges: list[tuple[Path, Path]] = []

    def recording_exchange(left, right):
        exchanges.append((left, right))
        assert left == app
        assert _is_patch_staged_app(right)
        assert patch_module._archive_state(archive) == "unpatched"
        assert patch_module._archive_state(
            right / "Contents" / "Resources" / "app.asar"
        ) == "patched"
        original_exchange(left, right)

    monkeypatch.setattr(patch_module, "_exchange_paths", recording_exchange)

    result = patch_desktop_app(
        app,
        tmp_path / "backups",
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "", ""
        ),
    )

    assert result.changed is True
    assert len(exchanges) == 1
    assert patch_module._archive_state(archive) == "patched"
    assert patch_module._bundle_identity(app).bundle_inode != (
        original_identity.bundle_inode
    )


def test_desktop_install_preserves_updater_change_after_exchange(
    tmp_path, monkeypatch
):
    app = _fake_app(tmp_path)
    archive = app / "Contents" / "Resources" / "app.asar"
    updated_archive = b"updater-after-exchange:" + ORIGINAL_AUTH_EXPRESSION
    original_exchange = patch_module._exchange_paths
    exchanges: list[tuple[Path, Path]] = []
    commands: list[list[str]] = []

    def updater_replaces_installed_app(left, right):
        original_exchange(left, right)
        exchanges.append((left, right))
        archive.write_bytes(updated_archive)

    monkeypatch.setattr(
        patch_module, "_exchange_paths", updater_replaces_installed_app
    )

    def updater_signature_runner(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(PatchError, match="changed after installation"):
        patch_desktop_app(
            app,
            tmp_path / "backups",
            runner=updater_signature_runner,
        )

    assert len(exchanges) == 1
    assert archive.read_bytes() == updated_archive
    assert any(
        command[-1] == str(app)
        and any(item.startswith("-R=") for item in command)
        for command in commands
    )


@pytest.mark.parametrize("operation", ["installation", "restoration"])
def test_bundle_operation_restores_prior_bundle_when_app_disappears_after_exchange(
    tmp_path, monkeypatch, operation
):
    app, backups, prior_archive = _prepare_bundle_operation(tmp_path, operation)
    archive = app / "Contents" / "Resources" / "app.asar"
    original_exchange = patch_module._exchange_paths
    exchanges: list[tuple[Path, Path]] = []

    def remove_installed_app(left, right):
        original_exchange(left, right)
        exchanges.append((left, right))
        shutil.rmtree(app)

    monkeypatch.setattr(patch_module, "_exchange_paths", remove_installed_app)

    with pytest.raises(
        PatchError, match=f"became unreadable after {operation}"
    ) as error:
        _run_bundle_operation(
            operation,
            app,
            backups,
            runner=lambda command, **kwargs: subprocess.CompletedProcess(
                command, 0, "", ""
            ),
        )

    assert not isinstance(error.value, patch_module._PreserveStagingError)
    assert len(exchanges) == 1
    assert archive.read_bytes() == prior_archive
    assert _preserved_staged_apps(tmp_path, operation) == []


@pytest.mark.parametrize("operation", ["installation", "restoration"])
def test_bundle_operation_preserves_unreadable_replacement_after_exchange(
    tmp_path, monkeypatch, operation
):
    app, backups, prior_archive = _prepare_bundle_operation(tmp_path, operation)
    archive = app / "Contents" / "Resources" / "app.asar"
    original_exchange = patch_module._exchange_paths
    exchanges: list[tuple[Path, Path]] = []

    def make_installed_app_unreadable(left, right):
        original_exchange(left, right)
        exchanges.append((left, right))
        if len(exchanges) == 1:
            archive.unlink()

    monkeypatch.setattr(
        patch_module, "_exchange_paths", make_installed_app_unreadable
    )

    with pytest.raises(
        patch_module._PreserveStagingError,
        match="failed replacement was preserved",
    ) as error:
        _run_bundle_operation(
            operation,
            app,
            backups,
            runner=lambda command, **kwargs: subprocess.CompletedProcess(
                command, 0, "", ""
            ),
        )

    assert not isinstance(error.value, patch_module._BundleRollbackError)
    assert len(exchanges) == 2
    assert archive.read_bytes() == prior_archive
    preserved = _preserved_staged_apps(tmp_path, operation)
    assert len(preserved) == 1
    assert not (preserved[0] / "Contents" / "Resources" / "app.asar").exists()


@pytest.mark.parametrize("operation", ["installation", "restoration"])
def test_bundle_operation_restores_prior_and_preserves_invalid_replacement(
    tmp_path, monkeypatch, operation
):
    app, backups, prior_archive = _prepare_bundle_operation(tmp_path, operation)
    archive = app / "Contents" / "Resources" / "app.asar"
    invalid_replacement = b"invalid-signature:" + ORIGINAL_AUTH_EXPRESSION
    original_exchange = patch_module._exchange_paths
    exchanges: list[tuple[Path, Path]] = []

    def replace_installed_app(left, right):
        original_exchange(left, right)
        exchanges.append((left, right))
        if len(exchanges) == 1:
            archive.write_bytes(invalid_replacement)

    def invalid_signature_runner(command, **kwargs):
        is_replacement_verification = (
            command[-1] == str(app)
            and any(item.startswith("-R=") for item in command)
        )
        if is_replacement_verification:
            return subprocess.CompletedProcess(
                command, 1, "", "replacement signature invalid"
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(patch_module, "_exchange_paths", replace_installed_app)

    with pytest.raises(
        patch_module._PreserveStagingError,
        match="replacement signature invalid",
    ) as error:
        _run_bundle_operation(
            operation, app, backups, runner=invalid_signature_runner
        )

    assert not isinstance(error.value, patch_module._BundleRollbackError)
    assert len(exchanges) == 2
    assert archive.read_bytes() == prior_archive
    preserved = _preserved_staged_apps(tmp_path, operation)
    assert len(preserved) == 1
    assert (
        preserved[0] / "Contents" / "Resources" / "app.asar"
    ).read_bytes() == invalid_replacement


@pytest.mark.parametrize("operation", ["installation", "restoration"])
def test_bundle_operation_preserves_staging_when_recovery_exchange_fails(
    tmp_path, monkeypatch, operation
):
    app, backups, prior_archive = _prepare_bundle_operation(tmp_path, operation)
    archive = app / "Contents" / "Resources" / "app.asar"
    invalid_replacement = b"rollback-failure:" + ORIGINAL_AUTH_EXPRESSION
    original_exchange = patch_module._exchange_paths
    exchanges: list[tuple[Path, Path]] = []

    def fail_recovery_exchange(left, right):
        exchanges.append((left, right))
        if len(exchanges) == 1:
            original_exchange(left, right)
            archive.write_bytes(invalid_replacement)
            return
        raise OSError("recovery exchange failed")

    def invalid_signature_runner(command, **kwargs):
        is_replacement_verification = (
            command[-1] == str(app)
            and any(item.startswith("-R=") for item in command)
        )
        if is_replacement_verification:
            return subprocess.CompletedProcess(
                command, 1, "", "replacement signature invalid"
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(patch_module, "_exchange_paths", fail_recovery_exchange)

    with pytest.raises(
        patch_module._BundleRollbackError, match="rollback failed"
    ):
        _run_bundle_operation(
            operation, app, backups, runner=invalid_signature_runner
        )

    assert len(exchanges) == 2
    assert archive.read_bytes() == invalid_replacement
    preserved = _preserved_staged_apps(tmp_path, operation)
    assert len(preserved) == 1
    assert (
        preserved[0] / "Contents" / "Resources" / "app.asar"
    ).read_bytes() == prior_archive


@pytest.mark.parametrize("operation", ["installation", "restoration"])
def test_bundle_operation_keeps_verified_bundle_when_displaced_is_unreadable(
    tmp_path, monkeypatch, operation
):
    app, backups, _ = _prepare_bundle_operation(tmp_path, operation)
    archive = app / "Contents" / "Resources" / "app.asar"
    verified_archive = (
        PATCHED_AUTH_EXPRESSION
        if operation == "installation"
        else ORIGINAL_AUTH_EXPRESSION
    )
    original_exchange = patch_module._exchange_paths
    exchanged_paths: list[tuple[Path, Path]] = []

    def corrupt_displaced_bundle(left, right):
        original_exchange(left, right)
        exchanged_paths.append((left, right))
        if len(exchanged_paths) == 1:
            (right / "Contents" / "Resources" / "app.asar").unlink()

    monkeypatch.setattr(
        patch_module, "_exchange_paths", corrupt_displaced_bundle
    )

    with pytest.raises(
        patch_module._PreserveStagingError,
        match="verified bundle remains installed",
    ):
        _run_bundle_operation(
            operation,
            app,
            backups,
            runner=lambda command, **kwargs: subprocess.CompletedProcess(
                command, 0, "", ""
            ),
        )

    assert len(exchanged_paths) == 1
    assert archive.read_bytes() == verified_archive
    preserved = _preserved_staged_apps(tmp_path, operation)
    assert len(preserved) == 1
    assert not (preserved[0] / "Contents" / "Resources" / "app.asar").exists()


@pytest.mark.parametrize("operation", ["installation", "restoration"])
def test_bundle_operation_keeps_verified_bundle_when_displaced_signature_is_invalid(
    tmp_path, monkeypatch, operation
):
    app, backups, _ = _prepare_bundle_operation(tmp_path, operation)
    archive = app / "Contents" / "Resources" / "app.asar"
    verified_archive = (
        PATCHED_AUTH_EXPRESSION
        if operation == "installation"
        else ORIGINAL_AUTH_EXPRESSION
    )
    invalid_displaced = b"invalid-displaced:" + ORIGINAL_AUTH_EXPRESSION
    original_exchange = patch_module._exchange_paths
    exchanged_paths: list[tuple[Path, Path]] = []

    def replace_live_before_exchange(left, right):
        if not exchanged_paths:
            archive.write_bytes(invalid_displaced)
        original_exchange(left, right)
        exchanged_paths.append((left, right))

    def invalid_displaced_runner(command, **kwargs):
        is_displaced_verification = (
            len(exchanged_paths) == 1
            and Path(command[-1]) != app
            and any(item.startswith("-R=") for item in command)
        )
        if is_displaced_verification:
            return subprocess.CompletedProcess(
                command, 1, "", "displaced signature invalid"
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(
        patch_module, "_exchange_paths", replace_live_before_exchange
    )

    with pytest.raises(
        patch_module._PreserveStagingError,
        match="displaced signature invalid",
    ):
        _run_bundle_operation(
            operation, app, backups, runner=invalid_displaced_runner
        )

    assert len(exchanged_paths) == 1
    assert archive.read_bytes() == verified_archive
    preserved = _preserved_staged_apps(tmp_path, operation)
    assert len(preserved) == 1
    assert (
        preserved[0] / "Contents" / "Resources" / "app.asar"
    ).read_bytes() == invalid_displaced


@pytest.mark.parametrize("operation", ["installation", "restoration"])
def test_bundle_operation_restores_signed_displaced_when_installed_is_unreadable(
    tmp_path, monkeypatch, operation
):
    app, backups, _ = _prepare_bundle_operation(tmp_path, operation)
    archive = app / "Contents" / "Resources" / "app.asar"
    signed_displaced = b"signed-updater:" + ORIGINAL_AUTH_EXPRESSION
    original_exchange = patch_module._exchange_paths
    exchanged_paths: list[tuple[Path, Path]] = []
    verified_displaced: list[list[str]] = []

    def updater_and_unreadable_install(left, right):
        if not exchanged_paths:
            archive.write_bytes(signed_displaced)
        original_exchange(left, right)
        exchanged_paths.append((left, right))
        if len(exchanged_paths) == 1:
            archive.unlink()

    def signed_displaced_runner(command, **kwargs):
        if (
            len(exchanged_paths) == 1
            and Path(command[-1]) != app
            and any(item.startswith("-R=") for item in command)
        ):
            verified_displaced.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(
        patch_module, "_exchange_paths", updater_and_unreadable_install
    )

    with pytest.raises(
        patch_module._PreserveStagingError,
        match="failed replacement was preserved",
    ):
        _run_bundle_operation(
            operation, app, backups, runner=signed_displaced_runner
        )

    assert len(exchanged_paths) == 2
    assert archive.read_bytes() == signed_displaced
    assert len(verified_displaced) == 1
    preserved = _preserved_staged_apps(tmp_path, operation)
    assert len(preserved) == 1
    assert not (preserved[0] / "Contents" / "Resources" / "app.asar").exists()


@pytest.mark.parametrize("operation", ["installation", "restoration"])
def test_bundle_operation_preserves_both_when_installed_and_displaced_are_unsafe(
    tmp_path, monkeypatch, operation
):
    app, backups, _ = _prepare_bundle_operation(tmp_path, operation)
    archive = app / "Contents" / "Resources" / "app.asar"
    unsafe_displaced = b"unsafe-displaced:" + ORIGINAL_AUTH_EXPRESSION
    original_exchange = patch_module._exchange_paths
    exchanged_paths: list[tuple[Path, Path]] = []

    def make_both_bundles_unsafe(left, right):
        archive.write_bytes(unsafe_displaced)
        original_exchange(left, right)
        exchanged_paths.append((left, right))
        archive.unlink()

    def unsafe_displaced_runner(command, **kwargs):
        is_displaced_verification = (
            len(exchanged_paths) == 1
            and Path(command[-1]) != app
            and any(item.startswith("-R=") for item in command)
        )
        if is_displaced_verification:
            return subprocess.CompletedProcess(
                command, 1, "", "displaced bundle unsafe"
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(patch_module, "_exchange_paths", make_both_bundles_unsafe)

    with pytest.raises(
        patch_module._PreserveStagingError,
        match="displaced bundle is also unsafe",
    ) as error:
        _run_bundle_operation(
            operation, app, backups, runner=unsafe_displaced_runner
        )

    assert len(exchanged_paths) == 1
    preserved = _preserved_staged_apps(tmp_path, operation)
    assert len(preserved) == 1
    assert str(preserved[0]) in str(error.value)
    assert (
        preserved[0] / "Contents" / "Resources" / "app.asar"
    ).read_bytes() == unsafe_displaced


@pytest.mark.parametrize("operation", ["installation", "restoration"])
def test_bundle_operation_preserves_staging_when_missing_app_restore_fails(
    tmp_path, monkeypatch, operation
):
    app, backups, prior_archive = _prepare_bundle_operation(tmp_path, operation)
    original_exchange = patch_module._exchange_paths
    original_replace = Path.replace
    exchange_complete = False
    staged_app: Path | None = None

    def remove_installed_app(left, right):
        nonlocal exchange_complete, staged_app
        original_exchange(left, right)
        staged_app = right
        exchange_complete = True
        shutil.rmtree(app)

    def fail_missing_app_restore(source, destination):
        if (
            exchange_complete
            and staged_app is not None
            and source == staged_app
            and Path(destination) == app
        ):
            raise OSError("missing app restore failed")
        return original_replace(source, destination)

    monkeypatch.setattr(patch_module, "_exchange_paths", remove_installed_app)
    monkeypatch.setattr(Path, "replace", fail_missing_app_restore)

    with pytest.raises(
        patch_module._BundleRollbackError, match="rollback failed"
    ):
        _run_bundle_operation(
            operation,
            app,
            backups,
            runner=lambda command, **kwargs: subprocess.CompletedProcess(
                command, 0, "", ""
            ),
        )

    assert not app.exists()
    preserved = _preserved_staged_apps(tmp_path, operation)
    assert len(preserved) == 1
    assert (
        preserved[0] / "Contents" / "Resources" / "app.asar"
    ).read_bytes() == prior_archive


def test_desktop_install_ignores_cleanup_failure_after_verified_exchange(
    tmp_path, monkeypatch
):
    app = _fake_app(tmp_path)
    archive = app / "Contents" / "Resources" / "app.asar"
    original_rmtree = shutil.rmtree
    failed_cleanups: list[Path] = []

    def failing_staged_cleanup(path, *args, **kwargs):
        target = Path(path)
        if _is_patch_staged_app(target):
            failed_cleanups.append(target)
            if kwargs.get("ignore_errors"):
                return None
            raise OSError("staged cleanup failed")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(patch_module.shutil, "rmtree", failing_staged_cleanup)

    result = patch_desktop_app(
        app,
        tmp_path / "backups",
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "", ""
        ),
    )

    assert result.changed is True
    assert len(failed_cleanups) == 1
    assert patch_module._archive_state(archive) == "patched"


def test_idempotent_install_never_labels_a_patched_bundle_as_original(tmp_path):
    app = _fake_app(tmp_path)
    backups = tmp_path / "backups"

    def successful_signer(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "", "")

    patch_desktop_app(app, backups, runner=successful_signer)
    full_backup = backups / "ChatGPT-26.825.32147-7303-original.app"
    shutil.rmtree(full_backup)

    result = patch_desktop_app(app, backups, runner=successful_signer)

    assert result.changed is False
    assert not full_backup.exists()


def test_idempotent_install_detects_archive_replacement_during_verification(
    tmp_path,
):
    app = _fake_app(tmp_path)
    backups = tmp_path / "backups"
    archive = app / "Contents" / "Resources" / "app.asar"

    patch_desktop_app(
        app,
        backups,
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "", ""
        ),
    )
    commands: list[list[str]] = []

    def replacing_verifier(command, **kwargs):
        commands.append(command)
        if "--verify" in command and command[-1] == str(app):
            archive.write_bytes(ORIGINAL_AUTH_EXPRESSION)
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(PatchError, match="changed during installation"):
        patch_desktop_app(app, backups, runner=replacing_verifier)

    assert archive.read_bytes() == ORIGINAL_AUTH_EXPRESSION
    assert not any("--force" in command for command in commands)


def test_idempotent_install_restores_an_invalidly_signed_patched_bundle(tmp_path):
    app = _fake_app(tmp_path)
    backups = tmp_path / "backups"
    full_backup = backups / "ChatGPT-26.825.32147-7303-original.app"
    full_backup.parent.mkdir()
    shutil.copytree(app, full_backup)
    (app / "Contents" / "Resources" / "app.asar").write_bytes(
        PATCHED_AUTH_EXPRESSION
    )

    def signature_runner(command, **kwargs):
        returncode = 1 if command[-1] == str(app) else 0
        return subprocess.CompletedProcess(command, returncode, "", "invalid signature")

    with pytest.raises(PatchError, match="restored the original"):
        patch_desktop_app(app, backups, runner=signature_runner)

    assert (app / "Contents" / "Resources" / "app.asar").read_bytes() == (
        ORIGINAL_AUTH_EXPRESSION
    )


def test_desktop_install_migrates_the_legacy_patch_without_bad_backup(tmp_path):
    app = _fake_app(tmp_path)
    backups = tmp_path / "backups"
    legacy_backup = backups / "ChatGPT-26.825.32147-original.app"
    legacy_backup.parent.mkdir()
    shutil.copytree(app, legacy_backup)
    (app / "Contents" / "Resources" / "app.asar").write_bytes(
        patch_module.LEGACY_PATCHED_AUTH_EXPRESSION
    )

    result = patch_desktop_app(
        app,
        backups,
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "", ""
        ),
    )

    assert result.changed is True
    assert (app / "Contents" / "Resources" / "app.asar").read_bytes() == (
        PATCHED_AUTH_EXPRESSION
    )
    assert not (
        backups / "ChatGPT-26.825.32147-7303-original.app"
    ).exists()


def test_restore_desktop_app_recovers_the_complete_original_bundle(tmp_path):
    app = _fake_app(tmp_path)
    backups = tmp_path / "backups"

    def successful_signer(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "", "")

    patch_desktop_app(app, backups, runner=successful_signer)

    restored_from = patch_module.restore_desktop_app(
        app, backups, runner=successful_signer
    )

    assert restored_from == (
        backups / "ChatGPT-26.825.32147-7303-original.app"
    )
    assert (app / "Contents" / "Resources" / "app.asar").read_bytes() == (
        ORIGINAL_AUTH_EXPRESSION
    )


def test_restore_desktop_app_ignores_cleanup_failure_after_exchange(
    tmp_path, monkeypatch
):
    app = _fake_app(tmp_path)
    backups = tmp_path / "backups"

    def successful_signer(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "", "")

    patch_desktop_app(app, backups, runner=successful_signer)
    original_rmtree = shutil.rmtree
    failed_cleanups: list[Path] = []

    def failing_staged_cleanup(path, *args, **kwargs):
        target = Path(path)
        is_restore_staged_app = (
            target.name == app.name
            and target.parent.name.startswith(f".{app.name}.restore-")
        )
        if is_restore_staged_app:
            failed_cleanups.append(target)
            if kwargs.get("ignore_errors"):
                return None
            raise OSError("restore cleanup failed")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(patch_module.shutil, "rmtree", failing_staged_cleanup)

    restored_from = patch_module.restore_desktop_app(
        app, backups, runner=successful_signer
    )

    assert restored_from == (
        backups / "ChatGPT-26.825.32147-7303-original.app"
    )
    assert len(failed_cleanups) == 1
    assert patch_module._archive_state(
        app / "Contents" / "Resources" / "app.asar"
    ) == "unpatched"


def test_restore_desktop_app_supports_the_legacy_version_only_backup(tmp_path):
    app = _fake_app(tmp_path)
    backups = tmp_path / "backups"
    legacy_backup = backups / "ChatGPT-26.825.32147-original.app"
    legacy_backup.parent.mkdir()
    shutil.copytree(app, legacy_backup)
    (app / "Contents" / "Resources" / "app.asar").write_bytes(
        PATCHED_AUTH_EXPRESSION
    )

    restored_from = patch_module.restore_desktop_app(
        app,
        backups,
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "", ""
        ),
    )

    assert restored_from == legacy_backup
    assert (app / "Contents" / "Resources" / "app.asar").read_bytes() == (
        ORIGINAL_AUTH_EXPRESSION
    )


def test_installer_runs_directly_from_outside_the_repository(tmp_path):
    script = Path(__file__).parents[1] / "local" / "patch-codex-desktop-offline.py"

    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Keep Codex Desktop" in completed.stdout


def test_installer_reports_filesystem_failures_without_a_traceback(tmp_path):
    script = Path(__file__).parents[1] / "local" / "patch-codex-desktop-offline.py"
    app = _fake_app(tmp_path)
    invalid_backup_root = tmp_path / "not-a-directory"
    invalid_backup_root.write_text("occupied")

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--app",
            str(app),
            "--backup-root",
            str(invalid_backup_root),
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "Codex Desktop offline patch failed:" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_cli_reports_install_success_and_already_installed(tmp_path):
    script = Path(__file__).parents[1] / "local" / "patch-codex-desktop-offline.py"
    app = _fake_app(tmp_path)
    backups = tmp_path / "backups"
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    fake_codesign = executable_dir / "codesign"
    fake_codesign.write_text("#!/bin/sh\nexit 0\n")
    fake_codesign.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{executable_dir}:{environment['PATH']}"
    command = [
        sys.executable,
        str(script),
        "--app",
        str(app),
        "--backup-root",
        str(backups),
    ]

    installed = subprocess.run(
        command, capture_output=True, text=True, env=environment
    )
    already_installed = subprocess.run(
        command, capture_output=True, text=True, env=environment
    )

    assert installed.returncode == 0, installed.stderr
    assert "Codex Desktop offline-auth patch installed." in installed.stdout
    assert "Archive backup:" in installed.stdout
    assert "Traceback" not in installed.stderr
    assert already_installed.returncode == 0, already_installed.stderr
    assert "Codex Desktop offline-auth patch is already installed." in (
        already_installed.stdout
    )
    assert "Traceback" not in already_installed.stderr


def test_cli_restore_exits_cleanly_after_restoring_the_bundle(tmp_path):
    script = Path(__file__).parents[1] / "local" / "patch-codex-desktop-offline.py"
    app = _fake_app(tmp_path)
    backups = tmp_path / "backups"
    legacy_backup = backups / "ChatGPT-26.825.32147-original.app"
    legacy_backup.parent.mkdir()
    shutil.copytree(app, legacy_backup)
    (app / "Contents" / "Resources" / "app.asar").write_bytes(
        PATCHED_AUTH_EXPRESSION
    )
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    fake_codesign = executable_dir / "codesign"
    fake_codesign.write_text("#!/bin/sh\nexit 0\n")
    fake_codesign.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{executable_dir}:{environment['PATH']}"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--app",
            str(app),
            "--backup-root",
            str(backups),
            "--restore",
        ],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Codex Desktop restored from:" in completed.stdout
    assert "Traceback" not in completed.stderr
