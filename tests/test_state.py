import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'common'))
import helper_state as s


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_moves_legacy_backup_once_without_copying(self):
        old = s.legacy_root(self.home)
        backup = old / ('transaction-' + 'a' * 32) / 'previous.bundle'
        backup.mkdir(parents=True)
        (backup / 'payload').write_text('synthetic original')
        inode = backup.stat().st_ino
        s.write_json(old / 'last-update.json', {'old': str(backup)})
        new = s.prepare_state_root(self.home)
        self.assertFalse(old.exists())
        moved = s.translate_legacy_path(backup, self.home)
        self.assertEqual(moved.stat().st_ino, inode)
        recorded = json.loads((new / 'last-update.json').read_text())['old']
        self.assertEqual(s.translate_legacy_path(recorded, self.home), moved)
        self.assertEqual(s.prepare_state_root(self.home), new)
        self.assertEqual(len(list(new.glob('transaction-*/previous.bundle'))), 1)

    def test_active_legacy_helper_blocks_migration(self):
        old = s.secure_directory(s.legacy_root(self.home))
        with s.file_lock(old / 'operation.lock'):
            with self.assertRaises(s.StateError):
                s.prepare_state_root(self.home)
        self.assertTrue(old.exists())
        self.assertFalse(s.state_root(self.home).exists())

    def test_conflicting_state_is_preserved_for_manual_review(self):
        old = s.secure_directory(s.legacy_root(self.home))
        new = s.secure_directory(s.state_root(self.home))
        (old / 'last-update.json').write_text('old state')
        (new / 'last-update.json').write_text('new state')
        with self.assertRaises(s.StateError):
            s.prepare_state_root(self.home)
        self.assertEqual((old / 'last-update.json').read_text(), 'old state')
        self.assertEqual((new / 'last-update.json').read_text(), 'new state')

    def test_final_directory_symlink_is_rejected(self):
        old = s.legacy_root(self.home)
        old.parent.mkdir(parents=True)
        elsewhere = self.home / 'elsewhere'
        elsewhere.mkdir()
        old.symlink_to(elsewhere)
        with self.assertRaises(s.StateError):
            s.prepare_state_root(self.home)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_state_directory_and_json_are_private(self):
        new = s.prepare_state_root(self.home)
        path = new / 'state.json'
        s.write_json(path, {'unicode': '原件'})
        self.assertEqual(new.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), {'unicode': '原件'})


if __name__ == '__main__':
    unittest.main()
