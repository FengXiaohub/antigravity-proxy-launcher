#!/usr/bin/python3
"""Build both helpers before replacing either; never edits the official app."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import shutil
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'common'))
from helper_state import file_lock, secure_directory, state_root, legacy_root, write_json

NAMES = ('Antigravity 代理启动.app', 'agy update.app')


class InstallError(RuntimeError):
    pass


def digest(path):
    h = hashlib.sha256()
    for entry in sorted(path.rglob('*')):
        h.update((str(entry.relative_to(path)) + '\0' + oct(stat.S_IMODE(entry.lstat().st_mode)) + '\0').encode())
        if entry.is_symlink():
            h.update(os.readlink(str(entry)).encode())
        elif entry.is_file():
            h.update(entry.read_bytes())
    return h.hexdigest()


def rename(source, target):
    os.rename(str(source), str(target))
    for parent in {source.parent, target.parent}:
        fd = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def sync_tree(root):
    # Flush newly built files before recording a durable installation commit.
    entries = [p for p in root.rglob('*') if not p.is_symlink()]
    for path in [p for p in entries if p.is_file()] + sorted(
            [p for p in entries if p.is_dir()], key=lambda p: len(p.parts), reverse=True) + [root]:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class Installer:
    def __init__(self, home=None, source=None, official=None):
        self.home = Path(os.environ.get('HOME', '')) if home is None else Path(home)
        self.source = Path(__file__).resolve().parent if source is None else Path(source)
        self.official = Path('/Applications/Antigravity.app') if official is None else Path(official)
        self.destination = self.home / 'Applications'
        self.root = self.home / 'Library/Application Support/AntigravityHelperInstaller'
        self.journal = self.root / 'install-state.json'
        self.stage = self.root / 'transaction'

    def command(self, args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise InstallError('构建或验证失败，请确认 Apple 命令行工具可用后重试。')
        return result

    def preflight(self):
        actual = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        if (os.geteuid() == 0 or not self.home.is_absolute() or self.home == Path('/') or
                self.home.resolve() != actual or self.destination.is_symlink()):
            raise InstallError('请使用当前普通账户安装；用户目录或 Applications 路径异常，未删除原程序。')
        if not self.official.is_dir():
            raise InstallError('请先从官方渠道把 Antigravity 安装到“应用程序”文件夹。')
        required = ('launcher/launch', 'launcher/launcher.py', 'launcher/Info.plist',
                    'updater/install.sh', 'updater/updater.py', 'updater/helper.applescript',
                    'common/helper_state.py', 'assets/launcher.icns', 'assets/updater.icns')
        for name in required:
            if not (self.source / name).is_file():
                raise InstallError('安装文件不完整，请重新下载整个仓库并解压，原程序未删除。')
        for name in ('launcher/launcher.py', 'updater/updater.py', 'common/helper_state.py'):
            compile((self.source / name).read_text(), name, 'exec')
        if self.destination.exists() and not os.access(str(self.destination), os.W_OK | os.X_OK):
            raise InstallError('当前账户无法写入自己的 Applications 文件夹，未替换原程序。')
        for name in NAMES:
            if (self.destination / name).is_symlink():
                raise InstallError('现有辅助图标是软链接，未覆盖，请联系维护人员确认原件位置。')

    def helpers_idle(self):
        result = self.command(['/bin/ps', '-axo', 'comm='])
        applet = str(self.destination / NAMES[1] / 'Contents/MacOS/applet')
        if applet in result.stdout.splitlines():
            raise InstallError('请先关闭 agy update 的弹窗，再重新安装；正在运行的 Antigravity 不会被关闭。')

    def build(self):
        launcher = self.stage / 'incoming' / NAMES[0]
        updater = self.stage / 'incoming' / NAMES[1]
        (launcher / 'Contents/MacOS').mkdir(parents=True)
        (launcher / 'Contents/Resources').mkdir()
        for source, target in [('launcher/launch', launcher / 'Contents/MacOS/launch'),
                               ('launcher/launcher.py', launcher / 'Contents/Resources/launcher.py'),
                               ('launcher/Info.plist', launcher / 'Contents/Info.plist'),
                               ('assets/launcher.icns', launcher / 'Contents/Resources/AppIcon.icns')]:
            shutil.copyfile(str(self.source / source), str(target))
        (launcher / 'Contents/MacOS/launch').chmod(0o755)
        self.command(['/usr/bin/osacompile', '-o', str(updater), str(self.source / 'updater/helper.applescript')])
        for source, target in [('updater/install.sh', updater / 'Contents/Resources/install.sh'),
                               ('updater/updater.py', updater / 'Contents/Resources/updater.py'),
                               ('assets/updater.icns', updater / 'Contents/Resources/applet.icns')]:
            shutil.copyfile(str(self.source / source), str(target))
        (updater / 'Contents/Resources/install.sh').chmod(0o755)
        for bundle in (launcher, updater):
            shutil.copyfile(str(self.source / 'common/helper_state.py'), str(bundle / 'Contents/Resources/helper_state.py'))
        assets = updater / 'Contents/Resources/Assets.car'
        if assets.exists():
            assets.unlink()
        plist = updater / 'Contents/Info.plist'
        data = plistlib.loads(plist.read_bytes())
        data.pop('CFBundleIconName', None)
        data['CFBundleName'] = 'agy update'
        plist.write_bytes(plistlib.dumps(data))
        self.command(['/usr/bin/codesign', '--force', '--sign', '-', '--identifier', 'agy update', str(updater)])
        self.verify(self.stage / 'incoming')

    def verify(self, directory):
        for name in NAMES:
            bundle = directory / name
            data = plistlib.loads((bundle / 'Contents/Info.plist').read_bytes())
            executable = bundle / 'Contents/MacOS' / data['CFBundleExecutable']
            if not executable.is_file() or not os.access(str(executable), os.X_OK):
                raise InstallError('辅助程序入口验证失败，未确认安装成功。')
            if not (bundle / 'Contents/Resources/helper_state.py').is_file():
                raise InstallError('辅助程序组件缺失，未确认安装成功。')
        self.command(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(directory / NAMES[1])])

    def recover(self):
        if self.stage.is_symlink():
            raise InstallError('安装暂存目录异常，已停止；没有删除恢复原件。')
        if not self.journal.exists():
            if self.stage.exists():
                shutil.rmtree(str(self.stage))
            return
        if self.journal.is_symlink():
            raise InstallError('安装恢复记录异常，已保留原件，请联系维护人员检查。')
        state = json.loads(self.journal.read_text())
        if set(state.get('entries', {})) != set(NAMES):
            raise InstallError('安装恢复记录不完整，已停止；没有删除恢复原件。')
        committed = state.get('phase') == 'committed'
        if committed:
            try:
                self.verify(self.destination)
                if any(digest(self.destination / name) != state['entries'][name]['new_digest'] for name in NAMES):
                    raise InstallError('安装结果与构建原件不一致。')
            except Exception:
                committed = False
        if not committed:
            for number, name in enumerate(NAMES):
                target = self.destination / name
                old = self.stage / ('old-' + str(number) + '.bundle')
                entry = state['entries'][name]
                if old.exists():
                    if target.exists():
                        shutil.rmtree(str(target))
                    rename(old, target)
                elif not entry['existed'] and target.exists():
                    shutil.rmtree(str(target))
                if entry['existed'] and (not target.is_dir() or digest(target) != entry['digest']):
                    raise InstallError('原辅助程序尚未完全恢复。请重新双击 Install.command；恢复原件已保留。')
        self.journal.unlink()
        if self.stage.exists():
            shutil.rmtree(str(self.stage))
        return 'committed' if committed else 'restored'

    def install(self):
        self.preflight()
        secure_directory(self.root)
        with contextlib.ExitStack() as stack:
            stack.enter_context(file_lock(self.root / 'install.lock'))
            roots = [p for p in (legacy_root(self.home), state_root(self.home)) if p.exists()]
            if not roots:
                roots = [secure_directory(state_root(self.home))]
            for root in roots:
                secure_directory(root)
                for lock in ('update.lock', 'operation.lock'):
                    stack.enter_context(file_lock(root / lock))
            self.helpers_idle()
            self.destination.mkdir(exist_ok=True)
            self.recover()
            try:
                self.build()
                sync_tree(self.stage / 'incoming')
                state = dict(phase='prepared', entries={})
                for name in NAMES:
                    target = self.destination / name
                    state['entries'][name] = dict(existed=target.exists(), digest=digest(target) if target.exists() else None,
                                                 new_digest=digest(self.stage / 'incoming' / name))
                write_json(self.journal, state)
                for number, name in enumerate(NAMES):
                    target = self.destination / name
                    if target.exists():
                        rename(target, self.stage / ('old-' + str(number) + '.bundle'))
                    rename(self.stage / 'incoming' / name, target)
                self.verify(self.destination)
                state['phase'] = 'committed'
                write_json(self.journal, state)
            except BaseException as exc:
                try:
                    outcome = self.recover()
                except Exception as recovery:
                    raise InstallError('安装失败，自动恢复未完成。请重新双击 Install.command 继续恢复；原件没有主动清除。') from recovery
                if outcome == 'committed':
                    raise InstallError('辅助程序已替换并通过验证，但安装收尾出现错误。请重新双击 Install.command 检查。') from exc
                raise InstallError('安装未完成，原来的辅助程序已保留或恢复。请检查安装文件和命令行工具后重试。') from exc
            self.recover()
        register = '/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister'
        if Path(register).is_file():
            for name in NAMES:
                try:
                    self.command([register, '-f', str(self.destination / name)])
                except InstallError:
                    print('提示：安装已完成，图标注册未完成，可从自己的 Applications 文件夹打开。')
        print('安装完成。两个原图标在 {}：'.format(self.destination))
        print('日常打开用「Antigravity 代理启动」；有已下载更新时用「agy update」。')


def main():
    try:
        Installer().install()
        return 0
    except Exception as exc:
        print('安装失败：' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
