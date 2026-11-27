#!/bin/sh
# Backend for the existing AppleScript applet. Results are one-line status messages.
HERE=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && /bin/pwd)
if [ ! -x /usr/bin/python3 ] || [ ! -f "$HERE/updater.py" ] || [ ! -f "$HERE/helper_state.py" ]; then
    /usr/bin/printf '%s\n' 'FAIL 更新助手组件不完整，请联系维护人员恢复原更新助手。'
    exit 0
fi
exec /usr/bin/python3 -B "$HERE/updater.py" "$@"
