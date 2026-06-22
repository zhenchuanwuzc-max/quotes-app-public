#!/bin/bash
# quotes-app 启动脚本（被 launchd 调用）
# v0.1.0：git 同步——server.py 在 iCloud 代码目录，数据 + sync.sh 在 ~/quotes-data/ git 仓
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# 启动时后台跑一次 git 同步：~/quotes-data/sync.sh 内部自带 flock + git pull/push timeout，
# 放后台确保它绝不拖死 server 启动（plan-reviewer：本地工具可用性不能被云同步绑死）。
SYNC_SH="$HOME/quotes-data/sync.sh"
if [ -f "$SYNC_SH" ]; then
    /bin/bash "$SYNC_SH" >> "$HOME/Library/Logs/quotes-sync.log" 2>&1 &
fi
# server.py 的 load_index_html() 自带 iCloud 占位符下载重试，无需在此阻塞等待。
exec /usr/bin/python3 "$SCRIPT_DIR/server.py"
