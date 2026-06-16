#!/bin/bash
# quotes-app 跨机同步（git，照抄 ~/daily-todo/sync.sh 范式）
# - pull 远端最新（quotes.json 走 JSON union 合并驱动，永不产生冲突标记）
# - 如有本地变化：commit + push
# - 全程写 /tmp/quotes-sync.log；最终状态写 /tmp/quotes-sync.status（供 /sync-now 读）
# 失败不阻塞 App
#
# 注意：本脚本运行在 ~/quotes/（数据 git 仓根），不是 iCloud 代码目录。
set -e
cd "$(dirname "$0")" || exit 0

# ⚠️ py2app 把 PYTHONHOME/PYTHONPATH 指向 bundle 内嵌解释器。本脚本（JSON 校验闸）和 git 的
# merge 合并驱动跑的是系统 python3，继承这俩变量会 "No module named encodings" 起不来 →
# 校验非零退出被误判成「quotes.json 非法 JSON」→ 后台同步永远中止、从不 commit。
# 清掉它们，让系统 python3 干净启动。（daily-todo py2app 化后后台同步一直没成的真因）
unset PYTHONHOME PYTHONPATH PYTHONEXECUTABLE

PY="$(command -v python3 || echo /usr/bin/python3)"
LOG="/tmp/quotes-sync.log"
STATUS="/tmp/quotes-sync.status"
TOKEN_FILE="$PWD/.gh-token"
REPO="zhenchuanwuzc-max/quotes-app-data"
log() { echo "[sync $(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG" 2>/dev/null || true; }
log "===== start @ $(hostname -s) ====="

# 自愈身份：换新机器时 git 没配 user.name/email，commit 会带自动推导的乱身份
if [ -z "$(git config user.name)" ]; then git config user.name "quotes-app"; fi
if [ -z "$(git config user.email)" ]; then git config user.email "quotes-app@localhost"; fi

# 自愈注册：确保 quotes.json 的 union 合并驱动已配置（每台设备每次 sync 幂等执行）
if [ -f json-merge.py ]; then
    git config merge.quotes-union.driver "$PY '$PWD/json-merge.py' %O %A %B" 2>/dev/null || true
    git config merge.quotes-union.name "quotes.json JSON-aware union merge" 2>/dev/null || true
fi

# 没配 remote 就跳过（首次安装时未联网或没有 GitHub 仓）
if ! git remote get-url origin > /dev/null 2>&1; then
    log "no remote, skip"; echo "ok: 无远端，跳过" > "$STATUS"; exit 0
fi

# SSH→HTTPS+PAT 兜底：新机没配 SSH key 时，用 .gh-token 把 remote 改成 HTTPS（daily-todo 缺这步）
# 仅当 remote 是 SSH 且 ssh 探测失败 + 有 token 时改写，避免每次 sync 都动 remote
ORIGIN_URL="$(git remote get-url origin 2>/dev/null || echo '')"
if echo "$ORIGIN_URL" | grep -q "^git@github.com:"; then
    if ! ssh -T -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes git@github.com 2>&1 | grep -q "successfully authenticated"; then
        if [ -f "$TOKEN_FILE" ]; then
            TOK="$(cat "$TOKEN_FILE" | tr -d '[:space:]')"
            if [ -n "$TOK" ]; then
                git remote set-url origin "https://${TOK}@github.com/${REPO}.git" 2>/dev/null || true
                log "SSH 不可用，已切 HTTPS+PAT remote"
            fi
        else
            log "SSH 不可用且无 .gh-token，push 大概率失败（保留本地，等下次）"
        fi
    fi
fi

# 拉远端（rebase 避免无意义 merge commit；quotes.json 冲突由 union 驱动自动并集）
if ! git pull --rebase --autostash origin main >> "$LOG" 2>&1; then
    log "pull/rebase FAILED — 放弃，保留本地"
    git rebase --abort 2>/dev/null || true
    git merge --abort 2>/dev/null || true
    echo "fail: 拉取失败（已保留本地，等下次）" > "$STATUS"; exit 0
fi
log "pull ok"

# 安全闸：quotes.json 必须是合法 JSON 才允许提交，绝不把坏文件推到远端
if [ -f quotes.json ] && ! "$PY" -c "import json;json.load(open('quotes.json'))" 2>/dev/null; then
    log "quotes.json 非法 JSON，中止本次同步（不污染远端）"
    echo "fail: quotes.json 非法 JSON" > "$STATUS"; exit 1
fi

# 看有没有本地未提交改动（仅追踪文件）
if [ -n "$(git status --porcelain)" ]; then
    git add -u
    # 新文件（首次）也纳入：quotes.json / json-merge.py / sync.sh / .gitattributes
    git add quotes.json json-merge.py sync.sh .gitattributes 2>/dev/null || true
    git commit -m "sync: $(date '+%Y-%m-%d %H:%M:%S') @ $(hostname -s)" >> "$LOG" 2>&1 || true
    if git push origin main >> "$LOG" 2>&1; then
        log "committed + pushed"; echo "ok: 已提交并推送" > "$STATUS"
    else
        log "push FAILED（已本地提交，等下次）"; echo "fail: 推送失败（已本地提交，等下次）" > "$STATUS"
    fi
else
    log "no local changes"; echo "ok: 已是最新" > "$STATUS"
fi
