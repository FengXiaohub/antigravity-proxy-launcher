#!/bin/sh
# Finder double-click entry; no commands need to be typed.
HERE=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && /bin/pwd)
OUTPUT=$(/bin/sh "$HERE/install.sh" 2>&1)
RESULT=$?
/usr/bin/printf '%s\n' "$OUTPUT"
if [ "$RESULT" -eq 0 ]; then TITLE="Antigravity 助手安装完成"; else TITLE="Antigravity 助手安装未完成"; fi
/usr/bin/osascript -e 'on run args
    tell current application to activate
    display alert (item 1 of args) message (item 2 of args)
end run' "$TITLE" "$OUTPUT" >/dev/null 2>&1 || true
exit "$RESULT"
