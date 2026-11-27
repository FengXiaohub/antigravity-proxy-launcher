"""Private, persistent state shared by the two existing helper applications."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import tempfile


class StateError(RuntimeError):
    pass


def state_root(home):
    return Path(home) / 'Library/Application Support/AntigravityHelper'


def legacy_root(home):
    return Path(home) / 'Library/Caches/AntigravityHelper'


def secure_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise StateError('辅助工具目录是软链接，已停止，请联系维护人员检查。')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid() or not path.is_dir():
        raise StateError('辅助工具目录不属于当前用户，已停止。')
    path.chmod(0o700)
    return path


@contextlib.contextmanager
def file_lock(path):
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StateError('另一个启动、更新或安装操作正在进行，请稍后重试。') from exc
        yield


def prepare_state_root(home):
    """Move legacy state once, under its original locks; never copy a backup."""
    destination = state_root(home)
    legacy = legacy_root(home)
    if legacy.is_symlink() or destination.is_symlink():
        raise StateError('辅助工具状态目录异常，已停止；没有删除备份。')
    if legacy.exists():
        secure_directory(legacy)
        try:
            with file_lock(legacy / 'update.lock'), file_lock(legacy / 'operation.lock'):
                if destination.exists():
                    secure_directory(destination)
                    # An empty cache left by an old helper is harmless. Never merge journals.
                    if any(p.name not in ('update.lock', 'operation.lock') for p in legacy.iterdir()):
                        raise StateError('发现两处辅助工具状态，请联系维护人员确认；没有合并或删除任何备份。')
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if legacy.stat().st_dev != destination.parent.stat().st_dev:
                        raise StateError('旧状态与新位置不在同一磁盘卷，未迁移或删除备份。')
                    os.rename(str(legacy), str(destination))
                    for parent in {legacy.parent, destination.parent}:
                        fd = os.open(str(parent), os.O_RDONLY)
                        try:
                            os.fsync(fd)
                        finally:
                            os.close(fd)
        except FileNotFoundError:
            # Another helper may have just completed the same locked rename.
            if legacy.exists() or not destination.exists():
                raise
    return secure_directory(destination)


def translate_legacy_path(path, home):
    """Old journal absolute paths remain recoverable after the directory move."""
    path = Path(path)
    try:
        return state_root(home) / path.relative_to(legacy_root(home))
    except ValueError:
        return path


def write_json(path, data):
    fd, name = tempfile.mkstemp(prefix='.state-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)
