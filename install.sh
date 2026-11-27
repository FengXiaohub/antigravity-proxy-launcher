#!/bin/sh
# Builds and verifies both existing helpers before replacing either one.
set -eu
die() { /usr/bin/printf '错误：%s\n' "$1" >&2; exit 1; }
case "${HOME-}" in
    ""|/|[!/]*) die "用户目录异常，安装已停止，没有删除原程序。" ;;
esac
HERE=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && /bin/pwd)
[ -x /usr/bin/python3 ] || die "Python 暂不可用，请先完成 Apple 命令行工具安装后重试。"
/usr/bin/python3 -B -c 'import fcntl, pathlib, plistlib, pwd, zipfile' || die "Python 暂不可用。若系统弹出命令行工具安装窗口，请完成安装后重新双击 Install.command。"
exec /usr/bin/python3 -B "$HERE/installer.py"
