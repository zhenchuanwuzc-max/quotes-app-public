#!/bin/bash
# quotes-app install.sh (v0.2.0：合并单仓版，2026-06-16)
#
# 与 v0.1.0（代码iCloud+数据独立仓）的区别：
#   - 代码 + 数据 + sync.sh + json-merge.py 全在 ~/quotes-app/（一个 git 仓，脚本所在处）
#   - 不再 clone/init 独立数据仓 ~/quotes（已合并，向 the reference app/another app 单仓范式看齐）
#   - server.py 用 SCRIPT_DIR 读同目录 quotes.json（DATA_DIR = 脚本所在目录）
#
# 设计：代码+数据同仓（2026-06-16 plan-reviewer v3 共识；拆两仓被双 kill_switch 否过）。
# 装机：git clone quotes-app 时数据已一起来，本脚本只配 venv + launchd，不另 clone 数据。

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.example.quotes-app"
PLIST_SRC="$SCRIPT_DIR/com.example.quotes-app.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_OUT="$HOME/Library/Logs/quotes-app.out.log"
LOG_ERR="$HOME/Library/Logs/quotes-app.err.log"

# 单仓：数据与代码同目录（SCRIPT_DIR = ~/quotes-app），不再有独立数据仓
DATA_DIR="$SCRIPT_DIR"
DATA_FILE="$DATA_DIR/quotes.json"
BACKUP_DIR="$SCRIPT_DIR/_backups"

echo "════════════════════════════════════════════"
echo "  quotes-app installer (v0.2.0 单仓)"
echo "════════════════════════════════════════════"
echo "  仓目录（代码+数据）: $SCRIPT_DIR"
echo ""

# ─────────────────────────────────────────────
# 1. 端口预检（8767 占用 fallback 8770）
# ─────────────────────────────────────────────
PORT=8767
if lsof -nP -iTCP:$PORT -sTCP:LISTEN > /dev/null 2>&1; then
    if launchctl list | grep -q "$LABEL"; then
        echo "✅ 端口 $PORT 被自己占用（重装场景）"
    else
        echo "⚠️  $PORT 被别的进程占用，fallback 8770"
        PORT=8770
        if lsof -nP -iTCP:$PORT -sTCP:LISTEN > /dev/null 2>&1; then
            echo "❌ 8770 也被占用，lsof -i:8770 排查后重试"; exit 1
        fi
    fi
fi
echo "✅ 端口选定：$PORT"

# ─────────────────────────────────────────────
# 2. 单仓：代码+数据已在 SCRIPT_DIR（git clone quotes-app 时一起来的）
#    无需独立 clone 数据仓。仅 pull 最新 + venv。
# ─────────────────────────────────────────────
cd "$SCRIPT_DIR"
if [ -d "$SCRIPT_DIR/.git" ]; then
    echo "✅ quotes-app 单仓 → pull 最新"
    git pull --rebase --autostash origin main 2>/dev/null || echo "  (pull 失败，保留本地，sync 会重试)"
fi

# venv + 依赖（pywebview/py2app 桌面 App 用；server 本身用系统 python3）
if [ ! -d "$SCRIPT_DIR/venv" ]; then
    echo "📦 建 venv + 装依赖（pywebview/py2app）"
    python3 -m venv venv
    ./venv/bin/pip install -q --upgrade pip 2>/dev/null || true
    ./venv/bin/pip install -q pywebview pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-WebKit py2app 2>/dev/null || true
fi

# 自愈 git config + 注册 merge 驱动（sync.sh 每次也会做，这里先做一遍保证首推干净）
if [ -z "$(git config user.name)" ]; then git config user.name "Your Name"; fi
if [ -z "$(git config user.email)" ]; then git config user.email "you@example.com"; fi
PY="$(command -v python3 || echo /usr/bin/python3)"
git config merge.quotes-union.driver "$PY '$SCRIPT_DIR/json-merge.py' %O %A %B" 2>/dev/null || true
git config merge.quotes-union.name "quotes.json JSON-aware union merge" 2>/dev/null || true
chmod +x "$SCRIPT_DIR/sync.sh" "$SCRIPT_DIR/json-merge.py" 2>/dev/null || true

# ─────────────────────────────────────────────
# 4. server launchd plist（替换占位符）
# ─────────────────────────────────────────────
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
chmod +x "$SCRIPT_DIR/start.sh"
if launchctl list | grep -q "$LABEL"; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
fi
python3 - <<PYEOF
src = open("$PLIST_SRC").read()
for k, v in {
    "__START_SH__": "$SCRIPT_DIR/start.sh",
    "__LOG_OUT__":  "$LOG_OUT",
    "__LOG_ERR__":  "$LOG_ERR",
    "__PORT__":     "$PORT",
}.items():
    src = src.replace(k, v)
open("$PLIST_DST", "w").write(src)
print(f"✅ server plist 写入 $PLIST_DST")
PYEOF
launchctl load "$PLIST_DST"
echo "✅ server launchd loaded"

# ─────────────────────────────────────────────
# 5. 同步 cron（每 10 分钟跑 ~/quotes-app/sync.sh，本地文件无需 brctl wrapper）
# ─────────────────────────────────────────────
SYNC_LABEL="com.example.quotes-sync"
SYNC_PLIST_SRC="$SCRIPT_DIR/com.example.quotes-sync.plist"
SYNC_PLIST_DST="$HOME/Library/LaunchAgents/$SYNC_LABEL.plist"
SYNC_LOG="$HOME/Library/Logs/quotes-sync.log"
if [ -f "$SYNC_PLIST_SRC" ]; then
    if launchctl list | grep -q "$SYNC_LABEL"; then
        launchctl unload "$SYNC_PLIST_DST" 2>/dev/null || true
    fi
    python3 - <<PYEOF
src = open("$SYNC_PLIST_SRC").read()
for k, v in {"__SYNC_RUN__": "$DATA_DIR/sync.sh", "__SYNC_LOG__": "$SYNC_LOG"}.items():
    src = src.replace(k, v)
open("$SYNC_PLIST_DST", "w").write(src)
print(f"✅ sync plist 写入 $SYNC_PLIST_DST")
PYEOF
    launchctl load "$SYNC_PLIST_DST"
    echo "✅ 同步 cron 已装（每 10 分钟跑 $DATA_DIR/sync.sh）"
fi

# ─────────────────────────────────────────────
# 6. curl 验活
# ─────────────────────────────────────────────
echo ""
echo "等 server 起来…"
for i in 1 2 3 4 5; do
    sleep 1
    if curl -sf "http://localhost:$PORT/health" > /dev/null 2>&1; then
        TOTAL=$(curl -s "http://localhost:$PORT/health" | python3 -c "import json,sys; print(json.load(sys.stdin).get('total', 0))")
        echo ""
        echo "════════════════════════════════════════════"
        echo "  ✅ quotes-app 装好了 (v0.1.0 git-sync)"
        echo "════════════════════════════════════════════"
        echo "  入口：http://localhost:$PORT/"
        echo "  数据：$DATA_FILE  ($TOTAL 条)"
        echo "  git：$(cd "$DATA_DIR" && git remote get-url origin 2>/dev/null | sed 's/:[^@]*@/:***@/' || echo '未配远端')"
        echo "  备份：$BACKUP_DIR"
        echo "  卸载：bash $SCRIPT_DIR/uninstall.sh"
        echo "════════════════════════════════════════════"
        exit 0
    fi
done
echo "❌ launchd 起来了但 curl 不通 → tail -30 $LOG_ERR"
exit 1
