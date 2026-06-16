#!/bin/bash
# quotes-app 同步 wrapper（被 launchd 调用）
# 为什么要 wrapper：launchd 直接跑 iCloud 里的 sync.py 时，文件可能是 dataless
# 占位符 → Operation not permitted(EPERM)。套一层 .sh（launchd 能稳定跑，install 时
# chmod +x 已 materialize），在 shell 上下文里先 brctl 拉下 sync.py 再跑，规避 EPERM。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
brctl download "$SCRIPT_DIR/sync.py" 2>/dev/null || true
sleep 1
exec /usr/bin/python3 "$SCRIPT_DIR/sync.py"
