import contextlib
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('helper_installer', REPO / 'installer.py')
i = importlib.util.module_from_spec(spec)
spec.loader.exec_module(i)


class FixtureInstaller(i.Installer):
    """Synthetic directories only; never builds, registers or launches a real app."""
    fail_build = False
    fail_final_verify = False

    def preflight(self):
        pass

    def command(self, args):
        return subprocess.CompletedProcess(args, 0, '', '')

    def build(self):
        if self.fail_build:
            raise i.InstallError('injected compiler/signature failure')
        for name in i.NAMES:
            directory = self.stage / 'incoming' / name
            directory.mkdir(parents=True)
            (directory / 'payload').write_text('new ' + name)

    def verify(self, directory):
        if directory == self.destination and self.fail_final_verify:
            raise i.InstallError('injected final verification failure')


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='助手安装 空格-')
        self.home = Path(self.temp.name)
        self.installer = FixtureInstaller(self.home)
        self.installer.destination.mkdir()
        for name in i.NAMES:
            directory = self.installer.destination / name
            directory.mkdir()
            (directory / 'payload').write_text('original ' + name)
        self.silent = patch('builtins.print')
        self.silent.start()

    def tearDown(self):
        self.silent.stop()
        self.temp.cleanup()

    def assert_originals(self):
        for name in i.NAMES:
            self.assertEqual((self.installer.destination / name / 'payload').read_text(), 'original ' + name)

    def test_success_replaces_both_and_leaves_no_extra_helper_copy(self):
        self.installer.install()
        for name in i.NAMES:
            self.assertEqual((self.installer.destination / name / 'payload').read_text(), 'new ' + name)
        self.assertFalse(self.installer.journal.exists())
        self.assertFalse(self.installer.stage.exists())
        self.assertEqual(set(p.name for p in self.installer.destination.iterdir()), set(i.NAMES))

    def test_build_or_sign_failure_preserves_both_originals(self):
        self.installer.fail_build = True
        with self.assertRaises(i.InstallError):
            self.installer.install()
        self.assert_originals()
        self.assertFalse(self.installer.stage.exists())

    def test_second_replacement_failure_restores_both_originals(self):
        original = i.rename
        def fail(source, target):
            if source == self.installer.stage / 'incoming' / i.NAMES[1]:
                raise OSError('injected second replacement failure')
            return original(source, target)
        with patch.object(i, 'rename', side_effect=fail):
            with self.assertRaises(i.InstallError):
                self.installer.install()
        self.assert_originals()
        self.assertFalse(self.installer.journal.exists())

    def test_final_verify_failure_restores_both_originals(self):
        self.installer.fail_final_verify = True
        with self.assertRaises(i.InstallError):
            self.installer.install()
        self.assert_originals()

    def test_corrupt_committed_result_restores_originals_before_discarding_them(self):
        original = self.installer.recover
        calls = [0]
        def interrupt_cleanup():
            calls[0] += 1
            if calls[0] > 1:
                raise SystemExit('process stopped before committed cleanup')
            return original()
        with patch.object(self.installer, 'recover', side_effect=interrupt_cleanup):
            with self.assertRaises(SystemExit):
                self.installer.install()
        self.assertTrue(self.installer.journal.exists())
        (self.installer.destination / i.NAMES[0] / 'payload').write_text('corrupted after commit')
        self.installer.recover()
        self.assert_originals()

    def test_hard_interruption_after_every_move_is_recoverable(self):
        for cut in range(1, 5):
            with self.subTest(move=cut):
                original = i.rename
                count = [0]
                def fail(source, target):
                    result = original(source, target)
                    count[0] += 1
                    if count[0] == cut:
                        raise SystemExit('simulated hard termination')
                    return result
                # Bypass the in-process exception recovery to model process death.
                original_recover = self.installer.recover
                recover_count = [0]
                def recover():
                    recover_count[0] += 1
                    if recover_count[0] > 1:
                        raise OSError('process already terminated')
                    return original_recover()
                with patch.object(i, 'rename', side_effect=fail), patch.object(self.installer, 'recover', side_effect=recover):
                    with self.assertRaises(i.InstallError):
                        self.installer.install()
                self.assertTrue(self.installer.journal.exists())
                self.installer.recover()
                self.assert_originals()
                self.assertFalse(self.installer.journal.exists())

    def test_failed_recovery_keeps_journal_and_original_for_next_attempt(self):
        original = i.rename
        def fail(source, target):
            if source == self.installer.stage / 'incoming' / i.NAMES[0] or source.name == 'old-0.bundle':
                raise OSError('injected move and recovery failure')
            return original(source, target)
        with patch.object(i, 'rename', side_effect=fail):
            with self.assertRaisesRegex(i.InstallError, '自动恢复未完成'):
                self.installer.install()
        self.assertTrue((self.installer.stage / 'old-0.bundle').is_dir())
        self.assertTrue(self.installer.journal.exists())
        self.installer.recover()
        self.assert_originals()

    def test_active_operation_prevents_install(self):
        root = i.secure_directory(i.state_root(self.home))
        with i.file_lock(root / 'operation.lock'):
            with self.assertRaises(RuntimeError):
                self.installer.install()
        self.assert_originals()

    def test_missing_source_is_detected_before_replacement(self):
        official = self.home / 'official-placeholder'
        official.mkdir()
        installer = i.Installer(self.home, self.home / 'missing-source', official)
        account = types.SimpleNamespace(pw_dir=str(self.home))
        with patch.object(i.pwd, 'getpwuid', return_value=account), patch.object(i.os, 'geteuid', return_value=501):
            with self.assertRaisesRegex(i.InstallError, '安装文件不完整'):
                installer.preflight()
        self.assert_originals()

    def test_abnormal_home_values_are_rejected(self):
        for home in ('', '/', 'relative', str(self.home / 'wrong-account')):
            with self.subTest(home=home), self.assertRaises(i.InstallError):
                i.Installer(home).preflight()
        self.assert_originals()

    def test_shell_entry_rejects_empty_home_without_running_python(self):
        result = subprocess.run(['/bin/sh', str(REPO / 'install.sh')],
                                env=dict(os.environ, HOME=''), capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('用户目录异常', result.stderr)
        self.assert_originals()


if __name__ == '__main__':
    unittest.main()
