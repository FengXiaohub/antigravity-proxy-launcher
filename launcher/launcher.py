#!/usr/bin/python3
"""One-shot launcher. Environment changes end at exec and its descendants."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from helper_state import prepare_state_root, state_root, write_json

APP = Path('/Applications/Antigravity.app')
EXEC = APP / 'Contents/MacOS/Antigravity'
ROOT = state_root(Path.home())
LOG = Path.home() / 'Library/Logs/AntigravityHelper.log'


def log(message):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(LOG), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a') as out:
            out.write(time.strftime('%Y-%m-%d %H:%M:%S') + ' [launcher] ' + message + '\n')
    except OSError:
        pass


def alert(message):
    # Arguments are data, never interpolated into AppleScript source.
    source = '''on run args
        tell current application to activate
        display alert "Antigravity 代理启动" message (item 1 of args) as warning
    end run'''
    subprocess.run(['/usr/bin/osascript', '-e', source, message],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def pids():
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,comm='],
                            capture_output=True, text=True, timeout=5, check=True)
    found = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[1] == str(EXEC):
            found.append(int(parts[0]))
    return found


def process_birth(pid):
    env = dict(os.environ, LC_ALL='C')
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='],
                            capture_output=True, text=True, timeout=5, env=env)
    return result.stdout.strip() if result.returncode == 0 else ''


def known_session(pid):
    path = ROOT / 'launch-session.json'
    try:
        if path.is_symlink():
            return False
        session = json.loads(path.read_text())
        return (isinstance(session, dict) and session.get('pid') == pid and
                bool(session.get('birth')) and session['birth'] == process_birth(pid))
    except (OSError, ValueError):
        return False


def candidates(snapshot):
    """Preserve every enabled local endpoint; a mixed port may offer both protocols."""
    values = []
    for prefix, protocol in [('HTTP', 'http'), ('HTTPS', 'http'), ('SOCKS', 'socks5h')]:
        enabled = re.search(r'^\s*' + prefix + r'Enable\s*:\s*1\s*$', snapshot, re.M)
        host = re.search(r'^\s*' + prefix + r'Proxy\s*:\s*(\S+)\s*$', snapshot, re.M)
        port = re.search(r'^\s*' + prefix + r'Port\s*:\s*(\d+)\s*$', snapshot, re.M)
        if enabled and host and port and host[1] in ('127.0.0.1', 'localhost', '::1'):
            number = int(port[1])
            if 0 < number < 65536:
                address = '[::1]' if host[1] == '::1' else '127.0.0.1'
                values.append((protocol, address, number))
    for port in (7890, 7891, 7893, 7897, 1080, 12000):
        values.extend([('http', '127.0.0.1', port), ('socks5h', '127.0.0.1', port)])
    return list(dict.fromkeys(values))


def find_proxy(snapshot, run=subprocess.run):
    listening = False
    probes = {}
    for protocol, host, port in candidates(snapshot):
        endpoint = (host, port)
        if endpoint not in probes:
            address = host.strip('[]')
            try:
                probes[endpoint] = run(['/usr/bin/nc', '-z', '-G', '1', address, str(port)],
                                       capture_output=True, timeout=2).returncode == 0
            except subprocess.TimeoutExpired:
                probes[endpoint] = False
        if not probes[endpoint]:
            continue
        listening = True
        # Go accepts socks5; curl's socks5h is used to test proxy-side DNS.
        proxy = '{}://{}:{}'.format(protocol, host, port)
        try:
            result = run(['/usr/bin/curl', '-q', '--silent', '--show-error', '--output', '/dev/null',
                          '--noproxy', '', '--proxy', proxy, '--connect-timeout', '5',
                          '--max-time', '12', '--write-out', '%{http_code}',
                          'https://www.google.com/generate_204'],
                         capture_output=True, text=True, timeout=14)
            if result.returncode == 0 and result.stdout.strip() == '204':
                return proxy.replace('socks5h://', 'socks5://'), listening
        except subprocess.TimeoutExpired:
            pass
    return None, listening


def main():
    global ROOT
    try:
        ROOT = prepare_state_root(Path.home())
        fd = os.open(str(ROOT / 'operation.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('正在检查或安装更新，请等更新窗口结束后再打开。')
            journal = ROOT / 'transaction.json'
            if journal.exists():
                phase = json.loads(journal.read_text()).get('phase')
                if phase != 'installed':
                    raise RuntimeError('上次更新尚未完成。请先双击原来的「agy update」恢复，再打开 Antigravity。')
            existing = pids()
            if existing:
                if not all(known_session(pid) for pid in existing):
                    alert('Antigravity 已经在运行，本次只能激活窗口，不能给现有进程补上代理。若它是从官方图标打开、登录或连接失败，请先按 Command-Q 完全退出，再点本启动器。')
                result = subprocess.run(['/usr/bin/open', '-a', str(APP)], capture_output=True, timeout=10)
                if result.returncode:
                    raise RuntimeError('Antigravity 已在运行，但窗口没有激活。请从 Dock 打开；若一直未响应，请先正常退出再试。')
                log('activated existing instance')
                return 0
            if not EXEC.is_file() or not os.access(EXEC, os.X_OK):
                raise RuntimeError('没有找到可运行的 Antigravity。请先双击「agy update」检查是否需要恢复。')
            snapshot = subprocess.run(['/usr/sbin/scutil', '--proxy'], capture_output=True,
                                      text=True, timeout=5, check=True).stdout
            proxy, listening = find_proxy(snapshot)
            if proxy is None:
                if listening:
                    raise RuntimeError('本地代理端口有响应，但 Google 连接预检在等待上限内未成功，或没有返回预期结果；这不代表所有 Google 服务都不可用。请先确认代理客户端（VPN 客户端）已连接，稍后重试；持续失败再检查节点。')
                raise RuntimeError('没有找到可用的本地代理。请先打开代理客户端（VPN 客户端），连接节点并开启「系统代理」，再双击重试。规则模式即可。')
            env = os.environ.copy()
            for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
                env[name] = proxy
            bypass = 'localhost,127.0.0.1,::1,*.local,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12,100.64.0.0/10'
            env['NO_PROXY'] = env['no_proxy'] = bypass
            write_json(ROOT / 'launch-session.json', dict(pid=os.getpid(), birth=process_birth(os.getpid())))
            log('proxy precheck passed; using ' + proxy)
            # Python-created descriptors are close-on-exec: no launcher or lock stays resident.
            os.execve(str(EXEC), [str(EXEC)] + sys.argv[1:], env)
    except Exception as exc:
        log('failed: ' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__))
        message = str(exc) if isinstance(exc, RuntimeError) else '启动没有完成，请稍后重试。若再次失败，请联系维护人员查看辅助工具日志。'
        alert(message)
        return 1


if __name__ == '__main__':
    sys.exit(main())
