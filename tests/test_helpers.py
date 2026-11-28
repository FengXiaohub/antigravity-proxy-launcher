import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import sys
import unittest
from unittest.mock import patch
import zipfile


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'common'))


def load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / name / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


u = load('updater')
l = load('launcher')


def fake_info(path):
    if not (path / 'release.txt').is_file():
        raise u.Failure('fixture missing or corrupt')
    return {'CFBundleIdentifier': 'com.google.antigravity',
            'CFBundleShortVersionString': (path / 'release.txt').read_text()}


class FakeUpdater(u.Updater):
    def __init__(self, home, app):
        super().__init__(home, app)
        self.ready = True
        self.stopped = False
        self.fail_postcheck = False
        self.external_installer = False

    def validate(self, path, expected=None, assess=True, official=True):
        found = fake_info(path)['CFBundleShortVersionString']
        u.version(found)
        if expected and expected != found:
            raise u.Failure('version mismatch')
        if self.fail_postcheck and path == self.app and found == '2.36.0':
            raise u.Failure('post-install verification failed')
        return found

    def processes(self):
        return []

    def installer_active(self):
        return self.external_installer

    def stop_app(self):
        self.stopped = True

    def run(self, args, timeout=90):
        if args[0] != '/usr/bin/open':
            raise AssertionError('Unexpected real command in isolated test: ' + str(args))
        return subprocess.CompletedProcess(args, 0, '', '')

    def backend_ready_now(self):
        return self.ready

    def wait_ready(self):
        return self.ready


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='helper-fixture-', dir=Path(__file__).parent)
        self.home = Path(self.temp.name)
        self.app = self.home / 'installed'
        self.candidate = u.state_root(self.home) / 'candidate'
        for path, ver in [(self.app, '2.35.1'), (self.candidate, '2.36.0')]:
            path.mkdir(parents=True)
            (path / 'release.txt').write_text(ver)
            (path / 'payload').write_text('synthetic data ' + ver)
        self.helper = FakeUpdater(self.home, self.app)
        marker = self.helper.launcher / 'Contents/MacOS/launch'
        marker.parent.mkdir(parents=True)
        marker.write_text('synthetic non-executable marker; no bundle metadata')
        self.token = 'a' * 32
        self.save_plan()
        self.info_patch = patch.object(u, 'info', fake_info)
        self.info_patch.start()

    def tearDown(self):
        self.info_patch.stop()
        self.temp.cleanup()

    def save_plan(self, **changes):
        plan = dict(token=self.token, created=u.time.time(), before='2.35.1', after='2.36.0',
                    candidate=str(self.candidate), digest=u.fingerprint(self.candidate))
        plan.update(changes)
        u.atomic_json(self.helper.plan, plan)

    def assert_old_intact(self):
        self.assertEqual((self.app / 'release.txt').read_text(), '2.35.1')

    def test_success_retains_original_and_has_no_journal(self):
        self.assertEqual(self.helper.install(self.token), 'OK 2.36.0')
        state = u.read_json(self.helper.last)
        self.assertEqual((Path(state['old']) / 'release.txt').read_text(), '2.35.1')
        self.assertFalse(self.helper.journal.exists())
        self.assertFalse(self.helper.plan.exists())

    def older_backup(self):
        path = self.helper.root / ('transaction-' + 'b' * 32) / 'previous.bundle'
        path.mkdir(parents=True)
        (path / 'release.txt').write_text('2.34.0')
        (path / 'payload').write_text('synthetic older version')
        u.atomic_json(self.helper.last, dict(token='b' * 32, before='2.34.0',
                      after='2.35.1', old=str(path), phase='healthy'))
        return path

    def test_retains_only_one_backup_even_when_startup_unconfirmed(self):
        older = self.older_backup()
        self.helper.ready = False
        self.assertTrue(self.helper.install(self.token).startswith('WARN '))
        self.assertFalse(older.exists())
        backups = list(self.helper.root.glob('transaction-*/previous.bundle'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'release.txt').read_text(), '2.35.1')

    def test_old_backup_cleanup_failure_prevents_new_backup_and_replacement(self):
        older = self.older_backup()
        with patch.object(u.shutil, 'rmtree', side_effect=PermissionError('injected cleanup failure')):
            with self.assertRaisesRegex(u.Failure, '当前应用尚未替换'):
                self.helper.install(self.token)
        self.assert_old_intact()
        self.assertTrue(older.exists())
        self.assertFalse(self.helper.journal.exists())
        self.assertEqual(len(list(self.helper.root.glob('transaction-*/previous.bundle'))), 1)

    def test_current_validation_failure_preserves_existing_backup(self):
        older = self.older_backup()
        with patch.object(self.helper, 'validate', side_effect=u.Failure('current app not verified')):
            with self.assertRaises(u.Failure):
                self.helper.install(self.token)
        self.assertTrue(older.exists())
        self.assert_old_intact()

    def test_confirmation_token_mismatch_does_not_stop_or_move(self):
        with self.assertRaises(u.Failure):
            self.helper.install('b' * 32)
        self.assertFalse(self.helper.stopped)
        self.assert_old_intact()

    def test_changed_candidate_does_not_stop_or_move(self):
        (self.candidate / 'payload').write_text('changed after confirmation')
        with self.assertRaises(u.Failure):
            self.helper.install(self.token)
        self.assertFalse(self.helper.stopped)
        self.assert_old_intact()

    def test_downgrade_is_rejected(self):
        (self.candidate / 'release.txt').write_text('2.34.0')
        self.save_plan(after='2.34.0')
        with self.assertRaises(u.Failure):
            self.helper.install(self.token)
        self.assertFalse(self.helper.stopped)
        self.assert_old_intact()

    def test_missing_launcher_does_not_stop_or_move(self):
        (self.helper.launcher / 'Contents/MacOS/launch').unlink()
        with self.assertRaises(u.Failure):
            self.helper.install(self.token)
        self.assertFalse(self.helper.stopped)
        self.assert_old_intact()

    def test_shipit_competition_does_not_move(self):
        self.helper.external_installer = True
        with self.assertRaises(u.Failure):
            self.helper.install(self.token)
        self.assertFalse(self.helper.stopped)
        self.assert_old_intact()

    def test_failed_new_move_restores_and_verifies(self):
        rename = os.rename
        def broken(source, target):
            if source == str(self.candidate):
                raise OSError('injected move failure')
            return rename(source, target)
        with patch.object(u.os, 'rename', side_effect=broken):
            with self.assertRaisesRegex(u.Failure, '已恢复并验证'):
                self.helper.install(self.token)
        self.assert_old_intact()
        self.assertFalse(self.helper.journal.exists())

    def test_failed_rollback_never_claims_restored(self):
        rename = os.rename
        def broken(source, target):
            if source == str(self.candidate) or source.endswith('previous.bundle'):
                raise OSError('injected restore failure')
            return rename(source, target)
        with patch.object(u.os, 'rename', side_effect=broken):
            with self.assertRaisesRegex(u.Failure, '自动恢复也未完成'):
                self.helper.install(self.token)
        self.assertTrue(self.helper.journal.exists())
        state = u.read_json(self.helper.journal)
        self.assertTrue(Path(state['old']).is_dir())
        self.assertFalse(self.app.exists())
        self.assertEqual(self.helper.recover(), 'RECOVERED 2.35.1')
        self.assert_old_intact()

    def test_interruption_after_first_move_recovers_next_time(self):
        rename = os.rename
        def interrupt(source, target):
            result = rename(source, target)
            if source == str(self.app):
                raise KeyboardInterrupt('simulated process termination')
            return result
        with patch.object(u.os, 'rename', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.helper.install(self.token)
        self.assertFalse(self.app.exists())
        self.assertEqual(self.helper.recover(), 'RECOVERED 2.35.1')
        self.assert_old_intact()

    def test_interruption_after_second_move_recovers_next_time(self):
        rename = os.rename
        def interrupt(source, target):
            result = rename(source, target)
            if source == str(self.candidate):
                raise KeyboardInterrupt('simulated process termination')
            return result
        with patch.object(u.os, 'rename', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.helper.install(self.token)
        self.assertEqual((self.app / 'release.txt').read_text(), '2.36.0')
        self.assertEqual(self.helper.recover(), 'RECOVERED 2.35.1')
        self.assert_old_intact()

    def test_post_install_verification_failure_restores(self):
        self.helper.fail_postcheck = True
        with self.assertRaisesRegex(u.Failure, '已恢复并验证'):
            self.helper.install(self.token)
        self.assert_old_intact()

    def test_unconfirmed_startup_is_warn_and_keeps_backup(self):
        self.helper.ready = False
        self.assertEqual(self.helper.install(self.token), 'WARN 2.36.0 ' + self.token)
        self.assertTrue(Path(u.read_json(self.helper.last)['old']).is_dir())
        self.assertEqual(self.helper.check(), 'WARN 2.36.0 ' + self.token)

    def test_completed_startup_is_recorded_healthy(self):
        self.helper.install(self.token)
        self.assertEqual(u.read_json(self.helper.last)['phase'], 'healthy')

    def test_running_process_blocks_interruption_recovery(self):
        rename = os.rename
        def interrupt(source, target):
            result = rename(source, target)
            if source == str(self.app):
                raise KeyboardInterrupt('simulated interruption')
            return result
        with patch.object(u.os, 'rename', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.helper.install(self.token)
        with patch.object(self.helper, 'app_processes', return_value=[123]):
            with self.assertRaises(u.Failure):
                self.helper.recover()
        self.assertTrue(self.helper.journal.exists())
        self.assertEqual(self.helper.recover(), 'RECOVERED 2.35.1')

    def test_explicit_rollback_uses_original(self):
        self.helper.install(self.token)
        self.assertEqual(self.helper.rollback(self.token), 'RESTORED 2.35.1')
        self.assert_old_intact()
        self.assertFalse(self.helper.last.exists())

    def test_other_operation_holds_lock(self):
        with self.helper.locked():
            with self.assertRaises(u.Failure):
                with u.Updater(self.home, self.app).locked():
                    self.fail('lock must be exclusive')

    def test_newest_version_is_selected_not_newest_timestamp(self):
        self.helper.shipit.mkdir(parents=True)
        for folder, release in [('update.z', '2.35.1'), ('update.a', '2.37.0'), ('update.b', '2.36.0')]:
            p = self.helper.shipit / folder / 'Antigravity.app'
            p.mkdir(parents=True)
            (p / 'release.txt').write_text(release)
        path, release = self.helper.choose('2.35.1')
        self.assertEqual(release, '2.37.0')
        self.assertIn('update.a', str(path))


    def test_manually_recovered_startup_unblocks_future_checks(self):
        self.helper.ready = False
        self.helper.install(self.token)
        self.helper.ready = True
        with patch.object(self.helper, 'choose', return_value=(None, 'NONE')) as choose:
            self.assertEqual(self.helper.check(), 'NONE 2.36.0')
            choose.assert_called_once_with('2.36.0')
        self.assertEqual(u.read_json(self.helper.last)['phase'], 'healthy')

    def test_missing_current_app_can_offer_and_restore_verified_backup(self):
        self.helper.install(self.token)
        u.shutil.rmtree(self.app)
        self.assertEqual(self.helper.check(), 'RESTORE_READY 2.35.1 ' + self.token)
        self.assertEqual(self.helper.rollback(self.token), 'RESTORED 2.35.1')
        self.assert_old_intact()

    def test_corrupt_current_app_can_offer_and_restore_verified_backup(self):
        self.helper.install(self.token)
        (self.app / 'release.txt').unlink()
        self.assertTrue(self.helper.check().startswith('RESTORE_READY '))
        self.helper.rollback(self.token)
        self.assert_old_intact()

    def test_corrupt_backup_is_never_offered_for_recovery(self):
        self.helper.install(self.token)
        state = u.read_json(self.helper.last)
        (Path(state['old']) / 'release.txt').unlink()
        u.shutil.rmtree(self.app)
        with self.assertRaises(u.Failure):
            self.helper.check()

    def test_malformed_current_version_does_not_block_verified_restore(self):
        self.helper.install(self.token)
        (self.app / 'release.txt').write_text('damaged-version')
        self.assertTrue(self.helper.check().startswith('RESTORE_READY '))
        self.helper.rollback(self.token)
        self.assert_old_intact()

    def test_foreign_bundle_identity_is_not_overwritten(self):
        self.helper.install(self.token)
        self.helper.stopped = False
        with patch.object(u, 'info', return_value={'CFBundleIdentifier': 'other.vendor.app',
                                                  'CFBundleShortVersionString': '2.36.0'}):
            with self.assertRaisesRegex(u.Failure, '其他流程更改'):
                self.helper.rollback(self.token)
        self.assertFalse(self.helper.stopped)

    def test_another_installed_version_is_not_overwritten_by_rollback(self):
        self.helper.install(self.token)
        (self.app / 'release.txt').write_text('2.37.0')
        self.helper.stopped = False
        with self.assertRaisesRegex(u.Failure, '其他流程更改'):
            self.helper.rollback(self.token)
        self.assertFalse(self.helper.stopped)
        self.assertEqual((self.app / 'release.txt').read_text(), '2.37.0')

    def test_interrupted_restore_after_original_is_back_finishes_cleanup(self):
        self.helper.install(self.token)
        rename = os.rename
        def interrupt(source, target):
            result = rename(source, target)
            if source.endswith('previous.bundle'):
                raise KeyboardInterrupt('interrupted after restoring original')
            return result
        with patch.object(u.os, 'rename', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.helper.rollback(self.token)
        self.assert_old_intact()
        self.assertEqual(self.helper.recover(), 'RECOVERED 2.35.1')
        self.assertFalse(self.helper.last.exists())
        self.assertFalse(self.helper.journal.exists())
        self.assertEqual(list(self.helper.root.glob('transaction-*/failed.bundle')), [])

    def test_write_permission_failure_does_not_stop_or_move_app(self):
        with patch.object(self.helper, 'ensure_writable', side_effect=u.Failure('permission denied')):
            with self.assertRaises(u.Failure):
                self.helper.install(self.token)
        self.assertFalse(self.helper.stopped)
        self.assert_old_intact()

    def test_first_move_permission_failure_leaves_no_journal_or_transaction(self):
        rename = os.rename
        def denied(source, target):
            if source == str(self.app):
                raise PermissionError(1, 'Operation not permitted (simulated App Management denial)')
            return rename(source, target)
        with patch.object(u.os, 'rename', side_effect=denied):
            with self.assertRaisesRegex(u.Failure, 'App 管理') as caught:
                self.helper.install(self.token)
        self.assertIn('未替换任何文件', str(caught.exception))
        self.assert_old_intact()
        self.assertFalse(self.helper.journal.exists())
        self.assertEqual(list(self.helper.root.glob('transaction-*')), [])
        # No pending transaction remains to block the launcher or the next check.
        self.assertIsNone(self.helper.recover())

class ValidationTests(unittest.TestCase):
    def test_normalized_duplicate_archive_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'package.zip'
            with zipfile.ZipFile(p, 'w') as z:
                z.writestr('Antigravity.app/Contents/payload', 'first')
                z.writestr('Antigravity.app//Contents/payload', 'second')
            with self.assertRaises(u.Failure):
                u.zip_info(p)

    def test_archive_expansion_limit_is_checked_before_reading_payload(self):
        entry = zipfile.ZipInfo('Antigravity.app/large')
        entry.file_size = 4 * 1024 ** 3 + 1
        with patch.object(u.zipfile, 'ZipFile') as archive:
            archive.return_value.__enter__.return_value.infolist.return_value = [entry]
            with self.assertRaisesRegex(u.Failure, '解压大小'):
                u.zip_info(Path('/unused'))
            archive.return_value.__enter__.return_value.read.assert_not_called()

    def test_sha512_base64_and_hex_and_corruption(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            p = Path(folder) / 'package.zip'
            p.write_bytes(b'not a real app')
            digest = hashlib.sha512(p.read_bytes()).digest()
            u.verify_zip(p, base64.b64encode(digest).decode())
            u.verify_zip(p, digest.hex())
            with self.assertRaises(u.Failure):
                u.verify_zip(p, base64.b64encode(bytes(64)).decode())

    def test_archive_traversal_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            p = Path(folder) / 'package.zip'
            with zipfile.ZipFile(p, 'w') as z:
                z.writestr('../outside', 'blocked')
            with self.assertRaises(u.Failure):
                u.zip_info(p)

    def test_signature_and_ticket_validation_do_not_trust_disabled_gatekeeper(self):
        helper = u.Updater()
        calls = []
        def run(args, timeout=90):
            calls.append(args)
            if args[0] == '/usr/bin/codesign':
                return subprocess.CompletedProcess(args, 0, '', '')
            if 'stapler' in args:
                return subprocess.CompletedProcess(args, 1, '', '')
            return subprocess.CompletedProcess(args, 0, 'assessments disabled', '')
        with patch.object(u, 'info', return_value={'CFBundleIdentifier': 'com.google.antigravity', 'CFBundleShortVersionString': '2.36.0'}), patch.object(helper, 'run', side_effect=run), patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'is_symlink', return_value=False):
            with self.assertRaisesRegex(u.Failure, '公证'):
                helper.validate(Path('/synthetic-candidate'))
        self.assertIn('--deep', calls[0])
        self.assertIn('--strict', calls[0])
        self.assertIn('EQHXZ8M8AV', calls[0][-2])
        self.assertFalse(any('--assess' in args for args in calls))

    def test_bad_signature_stops_before_notarization(self):
        helper = u.Updater()
        with patch.object(u, 'info', return_value={'CFBundleIdentifier': 'com.google.antigravity', 'CFBundleShortVersionString': '2.36.0'}), patch.object(helper, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'bad signature')) as run, patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'is_symlink', return_value=False):
            with self.assertRaisesRegex(u.Failure, '签名'):
                helper.validate(Path('/synthetic-candidate'))
            self.assertEqual(run.call_count, 1)

    def test_unknown_mode_is_safe(self):
        with patch.object(u, 'Updater') as helper, patch('builtins.print') as output:
            self.assertEqual(u.main(['install.sh', 'typo']), 0)
            helper.assert_not_called()
            self.assertIn('FAIL', output.call_args[0][0])


class LocalizedAppUpdateTests(unittest.TestCase):
    """Exercise real Updater.validate / verify_backup when the installed app is localized."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='localized-fixture-', dir=Path(__file__).parent)
        self.home = Path(self.temp.name)
        self.app = self.home / 'Antigravity.app'
        self.make_bundle(self.app, '2.35.1', localized=True)
        self.helper = u.Updater(self.home, self.app)
        marker = self.helper.launcher / 'Contents/MacOS/launch'
        marker.parent.mkdir(parents=True)
        marker.write_text('synthetic launcher marker')
        self.candidate = self.helper.shipit / 'update.new/Antigravity.app'
        self.make_bundle(self.candidate, '2.36.0', localized=False)
        self.candidate_signed = True
        self.helper.run = self.fake_run
        self.helper.processes = lambda: []
        self.helper.wait_ready = lambda: True
        self.helper.backend_ready_now = lambda: True

    def tearDown(self):
        self.temp.cleanup()

    def make_bundle(self, path, release, localized=False):
        (path / 'Contents/MacOS').mkdir(parents=True)
        (path / 'Contents/Resources').mkdir(parents=True)
        (path / 'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier': 'com.google.antigravity',
            'CFBundleExecutable': 'Antigravity',
            'CFBundleShortVersionString': release,
        }))
        binary = path / 'Contents/MacOS/Antigravity'
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
        (path / 'Contents/Resources/app.asar').write_text(
            'patched-zh-' + release if localized else 'official-en-' + release
        )
        if localized:
            (path / 'Contents/Resources/app.asar.bak').write_text('official-en-' + release)

    def fake_run(self, args, timeout=90):
        if args[0] == '/usr/bin/codesign':
            target = Path(args[-1])
            asar = (target / 'Contents/Resources/app.asar').read_text()
            ok = self.candidate_signed and asar.startswith('official-en-')
            return subprocess.CompletedProcess(args, 0 if ok else 1, '', '' if ok else 'ad-hoc or modified')
        if 'stapler' in args:
            return subprocess.CompletedProcess(args, 0, 'The validate action worked!', '')
        if args[0] == '/usr/bin/open':
            return subprocess.CompletedProcess(args, 0, '', '')
        raise AssertionError('Unexpected command in localized test: ' + str(args))

    def test_localized_installed_app_updates_directly_to_official_release(self):
        ready = self.helper.check()
        self.assertTrue(ready.startswith('READY 2.35.1 2.36.0 '))
        token = ready.split()[3]
        self.assertEqual(self.helper.install(token), 'OK 2.36.0')
        self.assertEqual((self.app / 'Contents/Resources/app.asar').read_text(), 'official-en-2.36.0')
        self.assertFalse((self.app / 'Contents/Resources/app.asar.bak').exists())
        state = u.read_json(self.helper.last)
        self.assertRegex(state['before_digest'], r'^[0-9a-f]{64}$')
        # If the user re-applies localization to 2.36.0, subsequent check() must not treat it as broken.
        (self.app / 'Contents/Resources/app.asar').write_text('patched-zh-2.36.0')
        (self.app / 'Contents/Resources/app.asar.bak').write_text('official-en-2.36.0')
        self.assertEqual(self.helper.check(), 'NONE 2.36.0')

    def test_localized_backup_rolls_back_intact_and_detects_backup_tampering(self):
        token = self.helper.check().split()[3]
        self.assertEqual(self.helper.install(token), 'OK 2.36.0')
        self.assertEqual(self.helper.rollback(token), 'RESTORED 2.35.1')
        self.assertEqual((self.app / 'Contents/Resources/app.asar').read_text(), 'patched-zh-2.35.1')
        self.assertTrue((self.app / 'Contents/Resources/app.asar.bak').is_file())

        # Re-run update and verify tampered localized backup is refused on rollback.
        self.make_bundle(self.candidate, '2.36.0', localized=False)
        token2 = self.helper.check().split()[3]
        self.assertEqual(self.helper.install(token2), 'OK 2.36.0')
        old = Path(u.read_json(self.helper.last)['old'])
        (old / 'Contents/Resources/app.asar').write_text('corrupted-backup')
        with self.assertRaisesRegex(u.Failure, '旧版备份完整性校验失败'):
            self.helper.rollback(token2)

    def test_unofficial_candidate_is_still_rejected_even_when_installed_app_is_localized(self):
        self.candidate_signed = False
        with self.assertRaisesRegex(u.Failure, 'Google 官方签名'):
            self.helper.check()

    def test_missing_executable_in_installed_app_offers_verified_localized_restore(self):
        token = self.helper.check().split()[3]
        self.assertEqual(self.helper.install(token), 'OK 2.36.0')
        (self.app / 'Contents/MacOS/Antigravity').unlink()
        self.assertEqual(self.helper.check(), 'RESTORE_READY 2.35.1 ' + token)
        self.assertEqual(self.helper.rollback(token), 'RESTORED 2.35.1')
        self.assertEqual((self.app / 'Contents/Resources/app.asar').read_text(), 'patched-zh-2.35.1')


class LauncherTests(unittest.TestCase):
    def test_session_marker_requires_matching_pid_and_birth(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(l, 'ROOT', Path(folder)), patch.object(l, 'process_birth', return_value='birth-now'):
            path = Path(folder) / 'launch-session.json'
            path.write_text(json.dumps(dict(pid=123, birth='birth-now')))
            self.assertTrue(l.known_session(123))
            self.assertFalse(l.known_session(124))
            path.write_text(json.dumps(dict(pid=123, birth='older-process')))
            self.assertFalse(l.known_session(123))
            path.write_text('[]')
            self.assertFalse(l.known_session(123))

    def test_all_system_candidates_whitespace_and_invalid_values(self):
        s = ' HTTPEnable : 1\n HTTPProxy : 127.0.0.1\n HTTPPort : 12000\n\tHTTPSEnable : 1\n HTTPSProxy : localhost\n HTTPSPort : 45678\n SOCKSEnable : 1\n SOCKSProxy : ::1\n SOCKSPort : 45679\n'
        c = l.candidates(s)
        self.assertEqual(c[:3], [('http', '127.0.0.1', 12000), ('http', '127.0.0.1', 45678), ('socks5h', '[::1]', 45679)])
        self.assertNotIn(('http', '127.0.0.1', 99999), l.candidates('HTTPEnable : 1\nHTTPProxy : 127.0.0.1\nHTTPPort : 99999\n'))

    def test_dead_http_does_not_hide_live_https(self):
        s = 'HTTPEnable : 1\nHTTPProxy : 127.0.0.1\nHTTPPort : 12000\nHTTPSEnable : 1\nHTTPSProxy : 127.0.0.1\nHTTPSPort : 45678\n'
        def run(args, **kwargs):
            if args[0].endswith('/nc'):
                return subprocess.CompletedProcess(args, 0 if args[-1] == '45678' else 1, '', '')
            return subprocess.CompletedProcess(args, 0, '204', '')
        self.assertEqual(l.find_proxy(s, run), ('http://127.0.0.1:45678', True))

    def test_ordinary_http_listener_is_not_a_proxy(self):
        def run(args, **kwargs):
            if args[0].endswith('/nc'):
                return subprocess.CompletedProcess(args, 0 if args[-1] in ('7890', '12000') else 1, '', '')
            proxy = args[args.index('--proxy') + 1]
            return subprocess.CompletedProcess(args, 0, '204' if proxy.endswith(':12000') else '200', '')
        self.assertEqual(l.find_proxy('', run)[0], 'http://127.0.0.1:12000')

    def test_pure_socks_proxy_keeps_correct_scheme(self):
        def run(args, **kwargs):
            if args[0].endswith('/nc'):
                return subprocess.CompletedProcess(args, 0 if args[-1] == '1080' else 1, '', '')
            proxy = args[args.index('--proxy') + 1]
            return subprocess.CompletedProcess(args, 0, '204' if proxy == 'socks5h://127.0.0.1:1080' else '000', '')
        self.assertEqual(l.find_proxy('', run)[0], 'socks5://127.0.0.1:1080')

    def test_live_but_offline_has_distinct_result(self):
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0 if args[0].endswith('/nc') else 28, '', '')
        self.assertEqual(l.find_proxy('', run), (None, True))

    def test_slow_available_proxy_is_not_rejected_at_four_seconds(self):
        s = 'HTTPEnable : 1\nHTTPProxy : 127.0.0.1\nHTTPPort : 12000\n'
        def run(args, **kwargs):
            if args[0].endswith('/nc'):
                return subprocess.CompletedProcess(args, 0, '', '')
            connect_limit = float(args[args.index('--connect-timeout') + 1])
            total_limit = float(args[args.index('--max-time') + 1])
            self.assertGreaterEqual(connect_limit, 5)
            self.assertGreater(kwargs['timeout'], total_limit)
            # A healthy response after 4.8 seconds exceeded the old total limit.
            return subprocess.CompletedProcess(args, 0 if total_limit > 4.8 else 28,
                                               '204' if total_limit > 4.8 else '000', '')
        self.assertEqual(l.find_proxy(s, run), ('http://127.0.0.1:12000', True))

    def test_probe_process_timeout_returns_control(self):
        def run(args, **kwargs):
            if args[0].endswith('/nc'):
                return subprocess.CompletedProcess(args, 0 if args[-1] == '12000' else 1, '', '')
            raise subprocess.TimeoutExpired(args, kwargs['timeout'])
        self.assertEqual(l.find_proxy('', run), (None, True))


if __name__ == '__main__':
    unittest.main(verbosity=2)
