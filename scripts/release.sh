#!/bin/bash
# quotes-app 上线脚本：测试全过才上线；上线后验活 + 线上只读浏览器检查，不过就自动退回上一版、重启、通知。
#
#   scripts/release.sh              测试 → 装合并驱动 → 重启 → 验活 → 线上浏览器检查（失败自动回滚）
#   scripts/release.sh --no-restart 只跑测试（不动线上）
#
# 测试（tests/，全部对着临时目录 + 随机端口的隔离 server，不碰真实数据、不联网）：
#   test_smoke     主流程冒烟          test_mutations  编辑/置顶/删除接口
#   test_merge     多机同步合并驱动    test_browser    真浏览器点一遍（新增→删除）
#
# 服务直接从本仓库工作区跑（launchd → start.sh → server.py），所以：
#   - 上线 = 当前提交；工作区有未提交改动就拒绝上线（否则线上跑的东西对不上任何提交，也没法回滚）
#   - 回滚 = 当前提交另存成分支 release-failed/<时间> → git reset --hard 到上一次上线成功的提交
#     （记在 .git/quotes-release-ok；没有记录时用 HEAD~1）→ 重启 → 再验活
#   - 合并驱动 json-merge.py 真正生效的是数据仓 ~/quotes-data 里那份（git 调的是它，sync.sh 会把它
#     提交并推给其他电脑）。上线时与代码仓不一致就覆盖过去，回滚时还原。
#
# 演练回滚：QUOTES_RELEASE_SIMULATE_FAIL=1 scripts/release.sh（线上检查强制判失败，走完整回滚流程）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LABEL="com.ocean.quotes-app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DATA_DIR="${QUOTES_DATA_DIR:-$HOME/quotes-data}"
START=$SECONDS

NO_RESTART=0
case "${1:-}" in
    "") ;;
    --no-restart) NO_RESTART=1 ;;
    *) echo "用法: $0 [--no-restart]" >&2; exit 2 ;;
esac

# 与 start.sh 保持同一个解释器（线上用 /usr/bin/python3）
PY=/usr/bin/python3
[ -x "$PY" ] || PY="$(command -v python3)"

notify() {  # macOS 通知，失败不影响退出码
    osascript -e "display notification \"$1\" with title \"quotes-app 上线\" sound name \"Basso\"" >/dev/null 2>&1 || true
}
cd "$ROOT"
git_() { git -C "$ROOT" "$@"; }

# ── 同一时间只允许一次上线 ──────────────────────────
LOCK="$HOME/Library/Caches/quotes-release.lock"
mkdir -p "$(dirname "$LOCK")"
until mkdir "$LOCK" 2>/dev/null; do
    if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then rmdir "$LOCK" 2>/dev/null || true; continue; fi
    echo "… 另一次上线在跑，排队等它"; sleep 5
done
TEST_LOG="$(mktemp -t quotes-release-test)"
trap 'rm -f "$TEST_LOG"; rmdir "$LOCK" 2>/dev/null || true' EXIT

# ── ① 跑全部测试 ─────────────────────────────────────
echo "▶ 跑测试（$PY）"
if ! "$PY" -m unittest discover -s tests -v >"$TEST_LOG" 2>&1; then
    cat "$TEST_LOG"
    echo "" >&2
    echo "✗ 测试失败，未上线。" >&2
    exit 1
fi
grep -E '\.\.\. (ok|FAIL|ERROR|skipped)' "$TEST_LOG" | sed 's/^/  /'
N="$(sed -n 's/^Ran \([0-9][0-9]*\) tests\{0,1\} in \(.*\)$/\1/p' "$TEST_LOG" | tail -1)"
T="$(sed -n 's/^Ran [0-9][0-9]* tests\{0,1\} in \(.*\)$/\1/p' "$TEST_LOG" | tail -1)"
SKIPPED="$(grep -c '\.\.\. skipped' "$TEST_LOG" || true)"
SUMMARY="${N:-?} 项全过（测试 ${T:-?}）"
[ "${SKIPPED:-0}" -gt 0 ] && SUMMARY="$SUMMARY，其中 ${SKIPPED} 项跳过"

if [ "$NO_RESTART" = 1 ]; then
    echo ""
    echo "✓ ${SUMMARY}，总耗时 $((SECONDS - START)) 秒（--no-restart：未动线上）"
    exit 0
fi

# ── ② 上线前检查 ─────────────────────────────────────
if [ -n "$(git_ status --porcelain --untracked-files=no)" ]; then
    echo "✗ 工作区有未提交的改动，先 commit 再上线（否则线上代码对不上提交，失败了也没法回滚）" >&2
    git_ status --short --untracked-files=no >&2
    exit 1
fi
HEAD_SHA="$(git_ rev-parse HEAD)"
OK_FILE="$(git_ rev-parse --git-path quotes-release-ok)"
case "$OK_FILE" in /*) ;; *) OK_FILE="$ROOT/$OK_FILE" ;; esac
PREV="$(cat "$OK_FILE" 2>/dev/null || true)"
if [ -z "$PREV" ] || ! git_ merge-base --is-ancestor "$PREV" "$HEAD_SHA" 2>/dev/null; then
    PREV="$(git_ rev-parse --verify -q 'HEAD~1' || true)"
    PREV_NOTE="（没有上次上线成功的记录，按上一个提交算）"
else
    PREV_NOTE="（上次上线成功的版本）"
fi

PORT="$(/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:QUOTES_PORT' "$PLIST" 2>/dev/null || true)"
PORT="${PORT:-8767}"
BASE="http://127.0.0.1:$PORT"
TARGET="gui/$(id -u)/$LABEL"
if ! launchctl print "$TARGET" >/dev/null 2>&1; then
    echo "✗ launchd 里没有 $LABEL（没装？先跑 install.sh）" >&2
    notify "launchd 里没有 $LABEL，没上线"
    exit 1
fi

service_pid() {
    launchctl print "$TARGET" 2>/dev/null | awk '$1=="pid" && $2=="=" {print $3; exit}' || true
}

# 重启并在 10 秒内验活：pid 换了 + 首页 200 + 列表接口 ok
restart_and_probe() {
    local old pid code
    old="$(service_pid)"
    launchctl kickstart -k "$TARGET"
    for _ in $(seq 1 20); do
        pid="$(service_pid)"
        if [ -n "$pid" ] && [ "$pid" != "$old" ]; then
            code="$(curl -s -m 2 --noproxy '*' -o /dev/null -w '%{http_code}' "$BASE/" || true)"
            if [ "$code" = "200" ] && curl -s -m 2 --noproxy '*' "$BASE/quotes?limit=1" \
                 | "$PY" -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("ok") else 1)' 2>/dev/null; then
                return 0
            fi
        fi
        sleep 0.5
    done
    return 1
}

# ── ③ 装合并驱动到数据仓 ─────────────────────────────
DRIVER_BAK=""
if [ -f "$DATA_DIR/json-merge.py" ] && ! cmp -s "$ROOT/json-merge.py" "$DATA_DIR/json-merge.py"; then
    DRIVER_BAK="$(mktemp -t quotes-json-merge)"
    cp -p "$DATA_DIR/json-merge.py" "$DRIVER_BAK"
    cp "$ROOT/json-merge.py" "$DATA_DIR/json-merge.py"
    chmod +x "$DATA_DIR/json-merge.py"
    echo "▶ 合并驱动已更新到 $DATA_DIR/json-merge.py（下次同步会推给其他电脑）"
fi

# ── ④ 重启 + 验活 + 线上只读浏览器检查 ─────────────────
echo "▶ 重启 $LABEL（$(git_ log --oneline -1)，端口 $PORT）"
FAIL=""
if ! restart_and_probe; then
    FAIL="重启后 10 秒内首页/列表接口不通"
else
    echo "▶ 线上只读浏览器检查"
    if ! "$PY" "$ROOT/tests/live_check.py" "$BASE"; then
        FAIL="线上浏览器检查没过"
    elif [ "${QUOTES_RELEASE_SIMULATE_FAIL:-}" = "1" ]; then
        FAIL="演练：假装线上检查没过"
    fi
fi

if [ -z "$FAIL" ]; then
    echo "$HEAD_SHA" > "$OK_FILE"
    [ -n "$DRIVER_BAK" ] && rm -f "$DRIVER_BAK"
    echo ""
    echo "✓ ${SUMMARY}；已上线并验活（$(git_ log --oneline -1)，pid=$(service_pid)），总耗时 $((SECONDS - START)) 秒"
    exit 0
fi

# ── ⑤ 回滚 ───────────────────────────────────────────
echo "✗ $FAIL" >&2
if [ -z "$PREV" ]; then
    echo "✗ 没有上一个提交可退，线上停在当前版本。看日志：tail -30 ~/Library/Logs/quotes-app.err.log" >&2
    notify "$FAIL，且没有可退回的版本"
    exit 1
fi
SAVED="release-failed/$(date '+%Y%m%d-%H%M%S')"
git_ branch -f "$SAVED" "$HEAD_SHA"
git_ reset -q --hard "$PREV"
if [ -n "$DRIVER_BAK" ]; then
    cp -p "$DRIVER_BAK" "$DATA_DIR/json-merge.py"
    rm -f "$DRIVER_BAK"
fi
echo "▶ 已退回 $(git_ log --oneline -1) ${PREV_NOTE}；没上成的版本存在分支 $SAVED（git reset --hard $SAVED 可取回）"
if restart_and_probe; then
    notify "$FAIL，已退回上一版 $(git_ rev-parse --short HEAD)"
    echo "✗ 上线失败，已自动退回上一版，线上服务正常。看日志：tail -30 ~/Library/Logs/quotes-app.err.log" >&2
else
    notify "$FAIL；退回上一版后服务仍不通，需要人工处理"
    echo "✗ 退回上一版后服务仍不通，需要人工处理：tail -30 ~/Library/Logs/quotes-app.err.log" >&2
fi
exit 1
