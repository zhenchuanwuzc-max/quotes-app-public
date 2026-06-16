#!/bin/bash
# quotes-app install.sh (v0.1.0：git 同步版)
#
# 与 v0.0.1（iCloud-jsonl）的区别：
#   - 数据 + sync.sh + json-merge.py 在 ~/quotes-data/（git 私有数据仓），不再走 iCloud _sync 中转
#   - 代码在 ~/quotes-app/（本脚本所在处），数据在 ~/quotes-data/，server 用 QUOTES_DATA_DIR env 连接
#   - 新机自动 git clone 数据仓（SSH 不通则用 .gh-token 切 HTTPS）
#   - 同步 cron 直接跑 ~/quotes/sync.sh（本地文件，无需 run_sync.sh brctl wrapper）
#
# 设计：代码与数据拆两仓（plan-reviewer 三盲共识）。本脚本是代码侧的 bootstrap。

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.example.quotes-app"
PLIST_SRC="$SCRIPT_DIR/com.example.quotes-app.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_OUT="$HOME/Library/Logs/quotes-app.out.log"
LOG_ERR="$HOME/Library/Logs/quotes-app.err.log"

DATA_DIR="$HOME/quotes-data"
DATA_FILE="$DATA_DIR/quotes.json"
BACKUP_DIR="$SCRIPT_DIR/_backups"
GH_REPO="zhenchuanwuzc-max/quotes-app-data"
GH_TOKEN_FILE="$DATA_DIR/.gh-token"

echo "════════════════════════════════════════════"
echo "  quotes-app installer (v0.1.0 git-sync)"
echo "════════════════════════════════════════════"
echo "  代码目录: $SCRIPT_DIR"
echo "  数据仓:   $DATA_DIR (git: $GH_REPO)"
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
# 2. 数据仓 bootstrap：~/quotes/ 是 git 仓吗？
#    - 已是 git 仓 → pull 最新
#    - 不是但有数据 → git init + 关联远端（首台机/迁移）
#    - 啥都没有 → git clone（新机）
# ─────────────────────────────────────────────
mkdir -p "$DATA_DIR"
# 拷一份 token 进数据仓（HTTPS fallback 用；.gitignore 已排除，不会进 git）
if [ ! -f "$GH_TOKEN_FILE" ] && [ -f "$HOME/daily-todo/.gh-token" ]; then
    cp "$HOME/daily-todo/.gh-token" "$GH_TOKEN_FILE" 2>/dev/null || true
fi

git_remote_url() {
    # 优先 SSH；探测失败且有 token 则 HTTPS+PAT
    if ssh -T -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes git@github.com 2>&1 | grep -q "successfully authenticated"; then
        echo "git@github.com:${GH_REPO}.git"
    elif [ -f "$GH_TOKEN_FILE" ]; then
        echo "https://$(tr -d '[:space:]' < "$GH_TOKEN_FILE")@github.com/${GH_REPO}.git"
    else
        echo "git@github.com:${GH_REPO}.git"   # 没 token 也给 SSH，让 git 自己报错
    fi
}

cd "$DATA_DIR"
if [ -d "$DATA_DIR/.git" ]; then
    echo "✅ ~/quotes 已是 git 仓 → pull 最新"
    git pull --rebase --autostash origin main 2>/dev/null || echo "  (pull 失败，保留本地，sync 会重试)"
elif [ -f "$DATA_FILE" ]; then
    echo "🆕 ~/quotes 有数据但非 git 仓 → git init + 关联远端（首台机/迁移）"
    git init -q
    git branch -M main 2>/dev/null || true
    git remote add origin "$(git_remote_url)" 2>/dev/null || git remote set-url origin "$(git_remote_url)"
    echo "  remote: $(git remote get-url origin | sed 's/:[^@]*@/:***@/')"
else
    echo "📥 ~/quotes 空 → git clone 数据仓（新机）"
    cd "$HOME"
    if git clone "$(git_remote_url)" "$DATA_DIR" 2>/dev/null; then
        echo "✅ clone 成功"
    else
        echo "⚠️  clone 失败（仓可能还没建 / 没权限）→ 先 git init 本地，等首推"
        mkdir -p "$DATA_DIR"; cd "$DATA_DIR"; git init -q; git branch -M main 2>/dev/null || true
        git remote add origin "$(git_remote_url)" 2>/dev/null || true
    fi
    cd "$DATA_DIR"
fi

# ─────────────────────────────────────────────
# 3. 同步脚本就位：把 sync.sh / json-merge.py / .gitattributes 拷进数据仓
#    （首台机从 iCloud 代码目录拷；新机 clone 已带，覆盖确保最新）
# ─────────────────────────────────────────────
for f in sync.sh json-merge.py; do
    if [ -f "$SCRIPT_DIR/$f" ]; then
        cp "$SCRIPT_DIR/$f" "$DATA_DIR/$f"
        chmod +x "$DATA_DIR/$f"
    fi
done
if [ ! -f "$DATA_DIR/.gitattributes" ]; then
    echo "quotes.json merge=quotes-union" > "$DATA_DIR/.gitattributes"
fi

# 自愈 git config + 注册 merge 驱动（sync.sh 每次也会做，这里先做一遍保证首推干净）
if [ -z "$(git config user.name)" ]; then git config user.name "quotes-app"; fi
if [ -z "$(git config user.email)" ]; then git config user.email "quotes-app@localhost"; fi
PY="$(command -v python3 || echo /usr/bin/python3)"
git config merge.quotes-union.driver "$PY '$DATA_DIR/json-merge.py' %O %A %B" 2>/dev/null || true
git config merge.quotes-union.name "quotes.json JSON-aware union merge" 2>/dev/null || true

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
# 5. 同步 cron（每 10 分钟跑 ~/quotes/sync.sh，本地文件无需 brctl wrapper）
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
