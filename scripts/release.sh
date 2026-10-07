#!/bin/bash
# quotes-app 上线脚本：测试全过才重启线上服务，重启后验活，不通就报警。
#   scripts/release.sh              跑测试 → 重启 launchd 服务 → 10 秒内验活
#   scripts/release.sh --no-restart 只跑测试（不重启、不验活）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LABEL="com.ocean.quotes-app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
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
    osascript -e "display notification \"$1\" with title \"quotes-app 上线失败\"" >/dev/null 2>&1 || true
}

# ── ① 跑测试 ─────────────────────────────────────────
echo "▶ 跑测试（$PY）"
TEST_LOG="$(mktemp -t quotes-release-test)"
trap 'rm -f "$TEST_LOG"' EXIT
cd "$ROOT"
if ! "$PY" -m unittest discover -s tests -v >"$TEST_LOG" 2>&1; then
    cat "$TEST_LOG"
    echo "" >&2
    echo "✗ 测试失败，未重启服务。" >&2
    exit 1
fi
cat "$TEST_LOG"
N="$(sed -n 's/^Ran \([0-9][0-9]*\) tests\{0,1\} in .*/\1/p' "$TEST_LOG" | tail -1)"
N="${N:-?}"

if [ "$NO_RESTART" = 1 ]; then
    echo ""
    echo "✓ ${N} 项全过，耗时 $((SECONDS - START)) 秒（--no-restart：未重启线上服务）"
    exit 0
fi

# ── ② 重启线上服务 ───────────────────────────────────
# 线上端口：优先读已安装 plist 里的 QUOTES_PORT，缺省 8767
PORT="$(/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:QUOTES_PORT' "$PLIST" 2>/dev/null || true)"
PORT="${PORT:-8767}"
BASE="http://127.0.0.1:$PORT"
UID_NUM="$(id -u)"
TARGET="gui/$UID_NUM/$LABEL"

service_pid() {
    launchctl print "$TARGET" 2>/dev/null | awk '$1=="pid" && $2=="=" {print $3; exit}' || true
}

if ! launchctl print "$TARGET" >/dev/null 2>&1; then
    echo "✗ launchd 里没有 $LABEL（没装？先跑 install.sh）" >&2
    notify "launchd 里没有 $LABEL"
    exit 1
fi

OLD_PID="$(service_pid)"
echo "▶ 重启 $LABEL（旧 pid=${OLD_PID:-无}，端口 $PORT）"
launchctl kickstart -k "$TARGET"

# ── ③ 10 秒内验活：pid 换了 + 首页 200 + 列表接口 ok ──
probe() {
    local pid code
    pid="$(service_pid)"
    [ -n "$pid" ] && [ "$pid" != "$OLD_PID" ] || return 1
    code="$(curl -s -m 2 --noproxy '*' -o /dev/null -w '%{http_code}' "$BASE/" || true)"
    [ "$code" = "200" ] || return 1
    curl -s -m 2 --noproxy '*' "$BASE/quotes?limit=1" | "$PY" -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("ok") else 1)' 2>/dev/null
}

ok=0
for _ in $(seq 1 20); do
    if probe; then ok=1; break; fi
    sleep 0.5
done
if [ "$ok" != 1 ]; then
    echo "✗ 重启后 10 秒内线上首页/列表接口不通（$BASE）。看日志：tail -30 ~/Library/Logs/quotes-app.err.log" >&2
    notify "重启后 10 秒内 $BASE 不通，看 ~/Library/Logs/quotes-app.err.log"
    exit 1
fi

echo ""
echo "✓ ${N} 项全过，耗时 $((SECONDS - START)) 秒；线上服务已重启并验活（新 pid=$(service_pid)）"
