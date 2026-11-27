"""Exercise actual AppleScript handlers with all dialogs and shell actions stubbed."""
from pathlib import Path
import re
import subprocess
import unittest


SOURCE = (Path(__file__).resolve().parents[1] / 'updater/helper.applescript').read_text()


def stub(source, pattern, replacement):
    result, count = re.subn(pattern, lambda _: replacement, source, flags=re.M)
    if count != 1:
        raise AssertionError('helper.applescript changed; stub did not match: ' + pattern)
    return result


def exercise(check, installed='OK 2.36.0', restored='RESTORED 2.35.1', restore=False, confirm='accept'):
    source = SOURCE.replace('set myPath to POSIX path of (path to me)', 'set myPath to "/unused/"')
    source = source.replace('\t\tactivate\n', '')
    source = re.sub(r'set chk to do shell script .*', 'set chk to "' + check + '"', source)
    source = re.sub(r'set inst to do shell script .*', 'set inst to "' + installed + '"', source)
    source = re.sub(r'set restored to do shell script .*', 'set restored to "' + restored + '"', source)
    if confirm == 'cancel':
        # A real cancel button raises error -128 instead of returning a button.
        update_stub = restore_stub = 'error number -128'
    elif confirm == 'decline':
        update_stub = 'set updateChoice to {button returned:"取消"}'
        restore_stub = 'set restoreChoice to {button returned:"取消"}'
    else:
        update_stub = 'set updateChoice to {button returned:"继续更新"}'
        restore_stub = 'set restoreChoice to {button returned:"恢复旧版本"}'
    source = stub(source, r'^\s*set updateChoice to display dialog .*$', '            ' + update_stub)
    source = stub(source, r'^\s*set restoreChoice to display dialog .*$', '            ' + restore_stub)
    choice = '恢复旧版本' if restore else '稍后重试'
    source = re.sub(r'set choice to display dialog .*', 'set choice to {button returned:"' + choice + '"}', source)
    source = source[:source.index('on notice(windowTitle')] + '''on notice(windowTitle, messageText, isWarning)
        return messageText
    end notice
    '''
    result = subprocess.run(['/usr/bin/osascript', '-'], input=source, text=True,
                            capture_output=True, timeout=5, check=True)
    if '操作未完成：' in result.stdout:
        raise AssertionError(result.stdout)
    return result.stdout


@unittest.skipUnless(Path('/usr/bin/osascript').exists(), 'AppleScript requires macOS')
class DialogLogicTests(unittest.TestCase):
    INSTALL_MARK = 'FAIL INSTALL_WAS_CALLED'
    ROLLBACK_MARK = 'FAIL ROLLBACK_WAS_CALLED'

    def test_confirmation_dialogs_declare_cancel_button(self):
        for prefix in ('set updateChoice to display dialog', 'set restoreChoice to display dialog'):
            line = next(l for l in SOURCE.splitlines() if l.strip().startswith(prefix))
            self.assertIn('cancel button "取消"', line)

    def assert_nothing_executed(self, output):
        self.assertNotIn('INSTALL_WAS_CALLED', output)
        self.assertNotIn('ROLLBACK_WAS_CALLED', output)
        self.assertNotIn('后台启动检查通过', output)
        self.assertNotIn('已恢复并校验旧版本', output)

    def test_cancel_update_never_installs(self):
        for confirm in ('cancel', 'decline'):
            with self.subTest(confirm=confirm):
                self.assert_nothing_executed(exercise('READY 2.35.1 2.36.0 ' + 'a' * 32,
                                                      self.INSTALL_MARK, self.ROLLBACK_MARK, confirm=confirm))

    def test_cancel_restore_never_rolls_back(self):
        for confirm in ('cancel', 'decline'):
            with self.subTest(confirm=confirm):
                self.assert_nothing_executed(exercise('RESTORE_READY 2.35.1 ' + 'a' * 32,
                                                      self.INSTALL_MARK, self.ROLLBACK_MARK, confirm=confirm))

    def test_missing_app_can_restore(self):
        self.assertIn('已恢复并校验旧版本', exercise('RESTORE_READY 2.35.1 ' + 'a' * 32))

    def test_restore_failure_is_explained(self):
        self.assertIn('模拟恢复失败', exercise('RESTORE_READY 2.35.1 ' + 'a' * 32, restored='FAIL 模拟恢复失败'))

    def test_no_package(self):
        self.assertIn('没有找到已下载', exercise('NONE 2.35.1'))

    def test_same_package_does_not_claim_latest(self):
        self.assertIn('本工具没有查询线上最新版本', exercise('SAME 2.35.1'))

    def test_older_package(self):
        self.assertIn('不会降级', exercise('OLDER 2.35.1'))

    def test_recovered(self):
        self.assertIn('已恢复并检查旧版本', exercise('RECOVERED 2.35.1'))

    def test_failure(self):
        self.assertIn('模拟失败', exercise('FAIL 模拟失败'))

    def test_confirmed_install_success(self):
        self.assertIn('后台启动检查通过', exercise('READY 2.35.1 2.36.0 ' + 'a' * 32))

    def test_confirmed_install_failure(self):
        self.assertIn('模拟安装失败', exercise('READY 2.35.1 2.36.0 ' + 'a' * 32, 'FAIL 模拟安装失败'))

    def test_startup_warning_can_restore(self):
        self.assertIn('已恢复并校验旧版本', exercise('READY 2.35.1 2.36.0 ' + 'a' * 32,
                      'WARN 2.36.0 ' + 'a' * 32, restore=True))

    def test_previous_startup_warning_can_restore(self):
        self.assertIn('已恢复并校验旧版本', exercise('WARN 2.36.0 ' + 'a' * 32, restore=True))

    def test_malformed_ready_never_installs(self):
        self.assertIn('检查结果不完整', exercise('READY 2.35.1'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
