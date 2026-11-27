#!/usr/bin/python3
"""Install already-downloaded updates; never edits official bundle contents."""
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from helper_state import StateError, prepare_state_root, state_root, translate_legacy_path, secure_directory

REQUIREMENT = ('anchor apple generic and identifier "com.google.antigravity" '
               'and certificate leaf[subject.OU] = "EQHXZ8M8AV" '
               'and certificate 1[field.1.2.840.113635.100.6.2.6] exists '
               'and certificate leaf[field.1.2.840.113635.100.6.1.13] exists')


class Failure(Exception):
    pass


def version(value):
    # Stable releases only. Unknown formats require review instead of guessing.
    if not isinstance(value, str) or not re.fullmatch(r'\d+(?:\.\d+){1,3}', value):
        raise Failure('版本格式无法识别，未进行更新，请联系维护人员检查。')
    numbers = tuple(int(n) for n in value.split('.'))
    return numbers + (0,) * (4 - len(numbers))


def atomic_json(path, data):
    fd, name = tempfile.mkstemp(prefix='.state-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(data, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path):
    try:
        if path.is_symlink():
            raise Failure('辅助工具状态文件异常，请联系维护人员检查。')
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise Failure('辅助工具状态无法读取，请联系维护人员检查；未自动删除任何备份。') from exc


def durable_move(source, target):
    # rename never nests source inside an existing directory, unlike shell mv.
    os.rename(str(source), str(target))
    for parent in {Path(source).parent, Path(target).parent}:
        fd = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def info(path):
    try:
        with (path / 'Contents/Info.plist').open('rb') as stream:
            return plistlib.load(stream)
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise Failure('无法读取应用版本，未进行替换。') from exc


def fingerprint(path):
    """Bind approval to all bytes, relative paths, modes, and symbolic-link targets."""
    digest = hashlib.sha256()
    for item in sorted(path.rglob('*')):
        relative = str(item.relative_to(path))
        meta = item.lstat()
        digest.update((relative + '\0' + oct(meta.st_mode) + '\0').encode())
        if item.is_symlink():
            digest.update(os.readlink(str(item)).encode())
        elif item.is_file():
            with item.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
    return digest.hexdigest()


def verify_zip(path, expected):
    try:
        wanted = bytes.fromhex(expected) if re.fullmatch(r'[0-9a-fA-F]{128}', expected) else base64.b64decode(expected, validate=True)
    except (ValueError, TypeError) as exc:
        raise Failure('更新包的 SHA-512 信息无效，未进行解压。') from exc
    if len(wanted) != 64:
        raise Failure('更新包的 SHA-512 信息无效，未进行解压。')
    digest = hashlib.sha512()
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
        after = os.fstat(stream.fileno())
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise Failure('更新包正在变化，请等下载完成后再试。')
    if digest.digest() != wanted:
        raise Failure('更新包 SHA-512 校验失败，请让 Antigravity 重新下载更新。')
    return (after.st_ino, after.st_size, after.st_mtime_ns)


def zip_info(path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 100000 or sum(e.file_size for e in entries) > 4 * 1024 ** 3:
            raise Failure('更新压缩包的文件数量或解压大小异常，未解压或安装。')
        seen = set()
        for entry in entries:
            name = PurePosixPath(entry.filename)
            normalized = str(name)
            if name.is_absolute() or '..' in name.parts or '\\' in entry.filename or normalized in seen:
                raise Failure('更新压缩包含异常路径，已停止。')
            seen.add(normalized)
            if not name.parts or name.parts[0] not in ('Antigravity.app', '__MACOSX'):
                raise Failure('更新压缩包内容不符合预期，已停止。')
            if stat.S_ISLNK(entry.external_attr >> 16):
                target = archive.read(entry).decode('utf-8')
                resolved = os.path.normpath(str(name.parent / target))
                if target.startswith('/') or resolved == '..' or resolved.startswith('../') or not resolved.startswith('Antigravity.app/'):
                    raise Failure('更新压缩包含越界链接，已停止。')
        return plistlib.loads(archive.read('Antigravity.app/Contents/Info.plist'))


class Updater:
    def __init__(self, home=None, app=None):
        self.home = Path.home() if home is None else Path(home)
        self.app = Path('/Applications/Antigravity.app') if app is None else Path(app)
        self.root = state_root(self.home)
        self.scratch = self.home / 'Library/Caches/AntigravityHelperDownloads'
        self.cache = self.home / 'Library/Caches/com.google.antigravity'
        self.shipit = self.home / 'Library/Caches/com.google.antigravity.ShipIt'
        self.launcher = self.home / 'Applications/Antigravity 代理启动.app'
        self.journal = self.root / 'transaction.json'
        self.plan = self.root / 'plan.json'
        self.last = self.root / 'last-update.json'

    def run(self, args, timeout=90):
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    def log(self, message):
        try:
            path = self.home / 'Library/Logs/AntigravityHelper.log'
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a') as stream:
                stream.write(time.strftime('%Y-%m-%d %H:%M:%S') + ' [update] ' + message + '\n')
        except OSError:
            pass

    @contextlib.contextmanager
    def locked(self, name='operation.lock'):
        prepare_state_root(self.home)
        fd = os.open(str(self.root / name), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Failure('另一个启动或更新操作正在进行，请稍后重试。') from exc
            yield

    def processes(self):
        result = self.run(['/bin/ps', '-axo', 'pid=,comm='], timeout=5)
        if result.returncode or not result.stdout.strip():
            raise Failure('无法确认应用是否已退出，未进行替换。')
        rows = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                rows.append((int(parts[0]), parts[1]))
        return rows

    def app_processes(self, backend=False):
        executables = {str(self.app / 'Contents/MacOS/Antigravity')}
        if backend:
            executables.add(str(self.app / 'Contents/Resources/bin/language_server'))
        return [pid for pid, command in self.processes() if command in executables]

    def installer_active(self):
        return any(Path(command).name == 'ShipIt' and
                   ('Antigravity.app/' in command or 'com.google.antigravity' in command)
                   for _, command in self.processes())

    def validate(self, path, expected=None, assess=True):
        if path.is_symlink() or not path.is_dir():
            raise Failure('更新包路径异常或已经被官方更新器移动，请稍后重新检查。')
        data = info(path)
        found = data.get('CFBundleShortVersionString')
        version(found)
        if data.get('CFBundleIdentifier') != 'com.google.antigravity' or (expected and found != expected):
            raise Failure('应用身份或版本与确认内容不一致，已停止。')
        result = self.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '-R', '=' + REQUIREMENT, str(path)])
        if result.returncode:
            raise Failure('Google 签名或应用完整性校验失败，未安装该更新包。')
        if assess:
            # A validated stapled ticket is independent of disabled Gatekeeper assessment.
            # Neither command changes policy or clears quarantine.
            ticket = self.run(['/usr/bin/xcrun', 'stapler', 'validate', str(path)], timeout=30)
            if ticket.returncode:
                status = self.run(['/usr/sbin/spctl', '--status'], timeout=10)
                if status.returncode or 'assessments enabled' not in status.stdout + status.stderr:
                    raise Failure('无法验证更新包的 Apple 公证凭证，未进行更新。请联网重试；若仍失败，请联系维护人员。')
                assessment = self.run(['/usr/sbin/spctl', '--assess', '--type', 'execute', '--ignore-cache', '--no-cache', '--verbose=2', str(path)], timeout=30)
                output = assessment.stdout + assessment.stderr
                if assessment.returncode or 'Notarized Developer ID' not in output or 'override=' in output:
                    raise Failure('无法确认更新包已通过 Apple 公证，未进行替换。请联网后重新检查。')
        return found

    def owned_path(self, path):
        path = translate_legacy_path(path, self.home)
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            raise Failure('恢复路径异常，已停止；备份未删除。')
        if (len(relative.parts) != 2 or relative.parts[1] != 'previous.bundle' or
                not re.fullmatch(r'transaction-[0-9a-f]{32}', relative.parts[0]) or
                self.root.resolve() not in path.resolve().parents or path.is_symlink()):
            raise Failure('恢复路径异常，已停止；备份未删除。')
        return path

    def ensure_writable(self):
        if not os.access(str(self.app.parent), os.W_OK | os.X_OK):
            raise Failure('当前账户没有替换 Antigravity 的权限。请让管理员通过访达处理应用的安装权限后重试；本次未关闭或替换应用。')

    def recovery_offer(self, state=None):
        if state is None:
            if not self.last.exists():
                raise Failure('当前 Antigravity 缺失或损坏，且没有可用恢复记录。请从官方渠道重新安装；会话数据未删除。')
            state = read_json(self.last)
        if not re.fullmatch(r'[0-9a-f]{32}', state.get('token', '')):
            raise Failure('恢复记录不完整，备份已保留，请联系维护人员检查。')
        self.validate(self.owned_path(state['old']), state['before'], assess=False)
        return 'RESTORE_READY {} {}'.format(state['before'], state['token'])

    def finish_restore(self, state):
        if self.journal.exists():
            self.journal.unlink()
        if self.last.exists() and read_json(self.last).get('token') == state.get('token'):
            self.last.unlink()
        failed = self.owned_path(state['old']).parent / 'failed.bundle'
        if failed.exists():
            try:
                shutil.rmtree(str(failed))
            except OSError:
                self.log('restored successfully; replacement cleanup deferred')
        self.log('restored and verified version ' + state['before'])

    def recover(self):
        if not self.journal.exists():
            return None
        state = read_json(self.journal)
        old = self.owned_path(state['old'])
        if self.app_processes(backend=True) or self.installer_active():
            raise Failure('上次更新未完成。请先正常退出 Antigravity，等官方更新结束后，再双击本图标恢复。')
        if old.exists():
            self.restore(state)
            return 'RECOVERED ' + state['before']
        # Journal was durable before moving the original; the first move never happened.
        if self.app.exists() and self.validate(self.app, state['before'], assess=False):
            self.finish_restore(state)
            return 'RECOVERED ' + state['before']
        raise Failure('发现未完成更新，但原件位置不符合预期。已停止；请联系维护人员恢复，备份未删除。')

    def restore(self, state):
        old = self.owned_path(state['old'])
        self.validate(old, state['before'], assess=False)
        self.ensure_writable()
        if self.app_processes(backend=True) or self.installer_active():
            raise Failure('应用或官方更新器仍在运行，未执行恢复。请正常退出后重试。')
        failed = old.parent / 'failed.bundle'
        if self.app.exists():
            if failed.exists():
                raise Failure('恢复暂存位置已占用，备份已保留，请联系维护人员处理。')
            durable_move(self.app, failed)
        try:
            durable_move(old, self.app)
        except OSError:
            if failed.exists() and not self.app.exists():
                durable_move(failed, self.app)
            raise Failure('旧版本未能恢复。备份仍保留，请联系维护人员处理。')
        self.validate(self.app, state['before'], assess=False)
        self.finish_restore(state)

    def choose(self, current):
        choices = []
        same = False
        for path in self.shipit.glob('update.*/Antigravity.app'):
            try:
                found = info(path).get('CFBundleShortVersionString')
                order = version(found)
            except Failure:
                continue
            same = same or order == version(current)
            if order > version(current):
                choices.append((order, str(path), found))
        if choices:
            _, path, found = max(choices)
            candidate = Path(path)
            if self.shipit.resolve() not in candidate.resolve().parents:
                raise Failure('官方暂存包路径异常，已停止。')
            self.validate(candidate, found)
            return candidate, found
        archive = self.cache / 'pending/Antigravity.zip'
        if archive.exists():
            if archive.is_symlink():
                raise Failure('更新压缩包路径异常，已停止。')
            metadata = read_json(self.cache / 'pending/update-info.json')
            expected = metadata.get('sha512')
            if not expected:
                raise Failure('更新包缺少 SHA-512 校验信息，请重新下载更新。')
            checked = verify_zip(archive, expected)
            data = zip_info(archive)
            found = data.get('CFBundleShortVersionString')
            if version(found) <= version(current):
                return None, 'SAME' if version(found) == version(current) else 'OLDER'
            secure_directory(self.scratch)
            extraction = Path(tempfile.mkdtemp(prefix='extract-', dir=str(self.scratch)))
            try:
                result = self.run(['/usr/bin/ditto', '-x', '-k', str(archive), str(extraction)], timeout=120)
                now = archive.stat()
                if result.returncode or checked != (now.st_ino, now.st_size, now.st_mtime_ns):
                    raise Failure('解压失败或更新包在检查期间发生变化，未进行安装。')
                candidate = extraction / 'Antigravity.app'
                self.validate(candidate, found)
            except Exception:
                shutil.rmtree(str(extraction))
                raise
            return candidate, found
        return None, 'SAME' if same else 'NONE'

    def check(self):
        with self.locked():
            recovered = self.recover()
            if recovered:
                return recovered
            if self.installer_active():
                raise Failure('官方更新器正在运行，请等它结束后再试。')
            try:
                current = info(self.app).get('CFBundleShortVersionString')
                version(current)
            except Failure:
                return self.recovery_offer()
            if self.last.exists():
                last = read_json(self.last)
                old = self.owned_path(last['old'])
                if last.get('after') == current and old.is_dir():
                    try:
                        self.validate(self.app, current, assess=False)
                    except Failure:
                        return self.recovery_offer(last)
                    if last.get('phase') == 'installed':
                        if not self.backend_ready_now():
                            return 'WARN {} {}'.format(current, last['token'])
                        last['phase'] = 'healthy'
                        atomic_json(self.last, last)
            if self.plan.exists():
                previous = read_json(self.plan)
                previous_path = translate_legacy_path(previous.get('candidate', ''), self.home)
                if (previous_path.name == 'Antigravity.app' and
                        previous_path.parent.parent in (self.root, self.scratch) and
                        previous_path.parent.name.startswith('extract-') and
                        not previous_path.parent.is_symlink()):
                    shutil.rmtree(str(previous_path.parent), ignore_errors=True)
                self.plan.unlink()
            candidate, found = self.choose(current)
            if candidate is None:
                return found + ' ' + current
            token = uuid.uuid4().hex
            atomic_json(self.plan, dict(token=token, created=time.time(), before=current,
                                        after=found, candidate=str(candidate), digest=fingerprint(candidate)))
            self.log('verified update available ' + current + ' -> ' + found)
            return 'READY {} {} {}'.format(current, found, token)

    def stop_app(self):
        for pid in self.app_processes():
            # Recheck exact executable before signaling each PID; never force-kill.
            if pid in self.app_processes():
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 25
        while self.app_processes(backend=True):
            if time.monotonic() >= deadline:
                raise Failure('Antigravity 没能正常退出。请按 Command-Q 完全退出后重试，未替换应用。')
            time.sleep(0.5)
        if self.installer_active():
            raise Failure('官方更新器接管了更新，请等它完成后重新检查；未手动替换应用。')

    def install(self, token):
        with self.locked():
            if self.journal.exists():
                return self.recover()
            plan = read_json(self.plan)
            if plan.get('token') != token or time.time() - plan.get('created', 0) > 1800:
                raise Failure('这次更新确认已过期，请关闭窗口后重新双击检查。')
            current = self.validate(self.app, plan['before'], assess=False)
            candidate = translate_legacy_path(plan['candidate'], self.home)
            if not any(root.resolve() in candidate.resolve().parents for root in (self.root, self.scratch, self.shipit)):
                raise Failure('更新暂存路径异常，已停止。')
            self.validate(candidate, plan['after'])
            if version(plan['after']) <= version(current) or fingerprint(candidate) != plan['digest']:
                raise Failure('更新包与刚才确认的内容不一致，请重新检查。')
            if not (self.launcher / 'Contents/MacOS/launch').is_file():
                raise Failure('原来的代理启动器不见了，未进行更新。请联系维护人员恢复启动器。')
            if self.installer_active():
                raise Failure('官方更新器正在运行，请等它完成后重试。')
            if len({self.app.stat().st_dev, candidate.stat().st_dev, self.root.stat().st_dev}) != 1:
                raise Failure('应用和更新包不在同一磁盘卷，已停止，以免更新中断导致文件不完整。')
            self.ensure_writable()
            self.stop_app()
            # Revalidate the exact candidate after app exit (which may trigger ShipIt).
            self.validate(candidate, plan['after'])
            if fingerprint(candidate) != plan['digest'] or self.installer_active() or self.app_processes(backend=True):
                raise Failure('更新文件或运行状态发生变化，未进行替换。请重新检查。')
            # Keep at most one previous original, including unconfirmed-startup cases.
            # The currently installed app remains intact while older backups are removed.
            self.validate(self.app, current, assess=False)
            try:
                self.prune_backups()
            except OSError as exc:
                raise Failure('更老的备份未能删除，已停止更新，当前应用尚未替换。请联系维护人员清理后重试。') from exc
            if self.installer_active() or self.app_processes(backend=True):
                raise Failure('应用或官方更新器重新启动了，未进行替换。请关闭后重试。')
            transaction = self.root / ('transaction-' + token)
            transaction.mkdir(mode=0o700)
            old = transaction / 'previous.bundle'
            state = dict(token=token, before=current, after=plan['after'], old=str(old), phase='prepared')
            atomic_json(self.journal, state)
            try:
                try:
                    durable_move(self.app, old)
                except OSError as exc:
                    # rename may succeed before a later fsync fails; only a move that never
                    # happened is treated as "nothing changed" and rolled back silently.
                    if old.exists() or not self.app.exists():
                        raise
                    self.discard_unstarted(transaction, token)
                    raise Failure('macOS 拒绝移动 Antigravity，本次未替换任何文件，原版本保持不变。'
                                  '请在「系统设置 → 隐私与安全性 → App 管理」中允许「agy update」，'
                                  '或确认 /Applications/Antigravity.app 不属于其他账户后，再双击「agy update」重试。'
                                  'Antigravity 已退出，可先用原来的代理启动图标重新打开。') from exc
                if self.installer_active() or self.app.exists():
                    raise Failure('官方更新器或其他进程正在改动应用，停止本次更新。')
                durable_move(candidate, self.app)
                self.validate(self.app, plan['after'], assess=False)
                if fingerprint(self.app) != plan['digest']:
                    raise Failure('安装后的文件与确认的更新包不一致。')
                state['phase'] = 'installed'
                atomic_json(self.journal, state)
            except Exception as exc:
                if old.exists():
                    try:
                        self.restore(state)
                    except Exception as recovery:
                        raise Failure('更新未完成，自动恢复也未完成。旧版原件已保留，请联系维护人员处理。') from recovery
                    raise Failure('更新未完成，已恢复并验证旧版本 ' + current + '。') from exc
                raise
            # Retain the previous original; never delete it merely because mv succeeded.
            atomic_json(self.last, state)
            self.journal.unlink()
            self.plan.unlink()
            self.log('installed and verified ' + current + ' -> ' + plan['after'])
        # Release the short operation lock before invoking the existing launcher.
        try:
            result = self.run(['/usr/bin/open', str(self.launcher)], timeout=10)
            ready = result.returncode == 0 and self.wait_ready()
        except Exception:
            ready = False
        if not ready:
            return 'WARN {} {}'.format(plan['after'], token)
        state['phase'] = 'healthy'
        atomic_json(self.last, state)
        try:
            self.prune_backups(token)
        except OSError:
            self.log('update healthy; older backup cleanup deferred')
        return 'OK ' + plan['after']

    def discard_unstarted(self, transaction, token):
        """The original never moved: drop only this attempt's journal and empty directory.

        Leftovers are harmless: recover() validates the untouched app and finishes cleanup.
        """
        try:
            if self.journal.exists() and read_json(self.journal).get('token') == token:
                self.journal.unlink()
            transaction.rmdir()  # rmdir refuses non-empty directories, so no bundle is deleted.
        except (OSError, Failure):
            self.log('first move refused; journal cleanup deferred to next check')

    def backend_ready_now(self):
        binary = str(self.app / 'Contents/Resources/bin/language_server')
        rows = self.processes()
        if not any(command == str(self.app / 'Contents/MacOS/Antigravity') for _, command in rows):
            return False
        for pid, command in rows:
            if command == binary:
                result = self.run(['/usr/sbin/lsof', '-a', '-p', str(pid), '-nP', '-iTCP', '-sTCP:LISTEN'], timeout=5)
                if result.returncode == 0 and 'LISTEN' in result.stdout:
                    return True
        return False

    def wait_ready(self):
        # lsof verifies live listeners owned by this backend, not old log lines or tokens.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.backend_ready_now():
                return True
            time.sleep(1)
        return False

    def prune_backups(self, keep=None):
        # Only this helper's transaction directories are eligible; no user data paths.
        for path in self.root.glob('transaction-*'):
            if (keep is None or path.name != 'transaction-' + keep) and re.fullmatch(r'transaction-[0-9a-f]{32}', path.name) and not path.is_symlink():
                if (path / 'previous.bundle').exists() or (path / 'failed.bundle').exists():
                    shutil.rmtree(str(path))
        if self.last.exists():
            state = read_json(self.last)
            if not self.owned_path(state['old']).exists():
                self.last.unlink()

    def rollback(self, token):
        with self.locked():
            state = read_json(self.last)
            if state.get('token') != token:
                raise Failure('恢复请求已失效，请联系维护人员检查现有备份。')
            # The replacement may be broken: authenticate the recovery original instead.
            self.validate(self.owned_path(state['old']), state['before'], assess=False)
            if self.app.exists():
                try:
                    current = info(self.app)
                except Failure:
                    current = None
                if current is not None:
                    identity = current.get('CFBundleIdentifier')
                    found = current.get('CFBundleShortVersionString')
                    try:
                        version(found)
                        recognizable = True
                    except Failure:
                        recognizable = False
                    if (identity not in (None, '', 'com.google.antigravity') or
                            (recognizable and found != state['after'])):
                        raise Failure('当前应用已被其他流程更改，未覆盖它；旧版备份仍保留，请联系维护人员确认。')
            self.ensure_writable()
            self.stop_app()
            state['phase'] = 'restoring'
            atomic_json(self.journal, state)
            self.restore(state)
        result = self.run(['/usr/bin/open', str(self.launcher)], timeout=10)
        return ('RESTORED ' if result.returncode == 0 else 'RESTORED_CLOSED ') + state['before']


def main(argv):
    mode = argv[1] if len(argv) > 1 else ''
    valid = ((mode == 'check' and len(argv) == 2) or
             (mode in ('install', 'rollback') and len(argv) == 3 and
              re.fullmatch('[0-9a-f]{32}', argv[2])))
    if not valid:
        print('FAIL 不支持的操作，请直接双击原来的 agy update 图标。')
        return 0
    updater = Updater()
    try:
        operation = argv[1]
        # A separate update lock spans startup verification; launcher only needs operation.lock.
        with updater.locked('update.lock'):
            result = updater.check() if operation == 'check' else getattr(updater, operation)(argv[2])
        words = result.split()
        updater.log('result=' + ' '.join(words[:2]))
        print(result)
    except Exception as exc:
        updater.log('failed: ' + (str(exc) if isinstance(exc, (Failure, StateError)) else type(exc).__name__))
        print('FAIL ' + (str(exc) if isinstance(exc, (Failure, StateError)) else '操作未完成。请重新双击检查恢复状态；若仍失败，请联系维护人员。'))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
