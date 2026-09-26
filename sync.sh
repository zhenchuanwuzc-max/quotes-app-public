#!/bin/bash
# quotes-app 跨机同步（git）
# 顺序：本地 commit → fetch（按线路、有硬超时）→ 本地 rebase → push。commit 在最前，因为它不需要网络。
# 日志 ~/Library/Logs/quotes-sync.log（本地盘持久，0600）；状态 /tmp/quotes-sync.status
#
# 注意：本脚本运行在 ~/quotes-data/（数据 git 仓根），不是 iCloud 代码目录。
#
# ============ 2026-08-30 事故复盘与修复 ============
# 现象：2026-06-22 ~ 08-30 完全没同步，launchd LastExitStatus 全程 0，无人察觉。
# 查实的根因链（每条都有实测证据）：
#   R1 旧版第 41-56 行「ssh 探测失败 → 把 remote 改写成 HTTPS+PAT」是**单向门**：
#      探测 ssh -T 实测 3.3~5.2 秒而阈值 ConnectTimeout=5 → 网络稍抖即假阴性；
#      改写后 remote 变 https://，第 44 行 `^git@github.com:` 判定再不成立 → 永远回不来。
#   R2 改写前不校验 PAT 对本仓是否真有权限。PAT 若未授权本仓，每次 pull 都 403，
#      而脚本把 403 当普通失败吞掉。
#   R3 launchd 环境**没有代理**（plist 只有 LANG/LC_ALL，launchctl getenv HTTPS_PROXY 为空），
#      HTTPS 直连 github.com 实测 75 秒超时失败；而 SSH 直连正常。
#      → 同一条命令 Ocean 在终端跑必成功（走 Clash），故障在手上无法复现。
#   R4 几乎所有失败路径都 exit 0 → launchd 与看板永远显示健康。
#   R5 commit 排在 pull 之后，pull 一坏本地就再不产生 commit。
#      实证：本机 reflog 在 06-22~08-30 之间完全空白，11 条新金句全程零版本化。
#   R6 push 单独失败时，commit 已使工作区变干净，下轮「工作区脏不脏」的门判假，
#      状态反写「ok: 已是最新」，远端恢复健康也不自愈。
#   R7 PAT 明文拼进 remote URL，git 报错时会把 URL 原样打印进日志 →
#      凭据不得进 URL，日志落盘前必须脱敏，且日志文件权限收到 0600。
#   R8 已注册的自定义 merge driver 退非 0 时，git **不补冲突标记**，把上游版本原样留在
#      工作区（合法 JSON，闸放行），add -u 会把它当成冲突解决结果提交 —— 本地新金句
#      只剩孤儿 stash。旧版全文除 --autostash 外没有任何一处检查 git stash。
#
# ============ 2026-09-25 网络层（照 ~/daily-todo-data/sync.sh 移植）============
# 现象：2026-09-23 家里电脑 GitHub SSH:22 间歇性卡死（连上不回话）。git 没有自带超时，
#   本脚本的 pull 挂了十几分钟；launchd 下一轮被锁挡住，/sync-now 45 秒超时只杀 bash、
#   git 和 ssh 子进程照样残留。
# 修法：
#   N1 每个联网动作（fetch / push / 推后复核）都走 run_to 硬超时，到点把 git 连同 ssh 整组杀。
#   N2 线路按「本机上次走通的（.git/sync-route，不进仓库）→ SSH:22 → SSH over 443
#      → 443 走系统代理」依次试。只改传输（GIT_SSH_COMMAND / -c http.proxy），不改 remote URL。
#      SYNC_ROUTES 环境变量可强制线路顺序（排障用）。
#   N3 pull --rebase 拆成「有超时的 fetch」+「本地 rebase」，连不上和合并失败分开报。
#   N4 取消旧版 pull/push 失败时在 SSH ⇄ HTTPS+PAT 之间改写 remote URL 的自愈（R1/R2 那套）：
#      它由线路层取代，且改 remote 本身就是 R1 单向门的来源。本脚本不再读 .gh-token；
#      若某台机的 origin 已是 https（旧版切过去的），走 https / https-proxy 线路，日志照旧脱敏（R7）。
set -e
cd "$(dirname "$0")" || exit 0

# py2app 把 PYTHONHOME/PYTHONPATH 指向 bundle 内嵌解释器。本脚本（JSON 校验闸）和 git 的
# merge 合并驱动跑的是系统 python3，继承这俩变量会 "No module named encodings" 起不来 →
# 校验非零退出被误判成「quotes.json 非法 JSON」→ 后台同步永远中止、从不 commit。
unset PYTHONHOME PYTHONPATH PYTHONEXECUTABLE

PY="$(command -v python3 || echo /usr/bin/python3)"

# 日志落本地盘：旧版写 /tmp，重启即清 —— 复盘时只剩 3 天可查，两个月失败历史全没了。
LOG_DIR="$HOME/Library/Logs"
mkdir -p "$LOG_DIR" 2>/dev/null || true
LOG="$LOG_DIR/quotes-sync.log"
FAILCOUNT="$LOG_DIR/.quotes-sync-failcount"
touch "$LOG" 2>/dev/null || true
chmod 600 "$LOG" 2>/dev/null || true   # R7：日志可能含凭据痕迹，不给同机其他进程读

# STATUS 必须留在 /tmp —— server.py 的 /sync-now 端点硬编码读这个路径（搜 quotes-sync.status），
# 且它是「最近一次结果」的瞬时状态，重启清掉语义正确。别去"统一"它，会打断 App 里的同步按钮。
STATUS="/tmp/quotes-sync.status"

LOCK="$PWD/.synclock"
HOLD_LOCK=0

log() { echo "[sync $(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG" 2>/dev/null || true; }

# R7：任何落盘的文本都先过脱敏，杜绝 PAT 明文进日志
redact() { sed -E 's#(github_pat_|ghp_|gho_|ghs_|ghu_|ghr_)[A-Za-z0-9_]+#\1<REDACTED>#g; s#//[^@/]*@#//<REDACTED>@#g'; }
# 跑 git 并把输出脱敏后写日志，返回 git 自己的退出码
gitq() { "$@" 2>&1 | redact >> "$LOG"; return "${PIPESTATUS[0]}"; }

# 日志轮转：超 2MB 截到尾部 800 行（本地盘不会被系统清，得自己管）
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 2097152 ]; then
    tail -n 800 "$LOG" > "$LOG.rot" 2>/dev/null && mv "$LOG.rot" "$LOG" 2>/dev/null || true
    chmod 600 "$LOG" 2>/dev/null || true
    log "（日志超 2MB 已轮转，保留尾部 800 行）"
fi

# ---- 并发锁（macOS 无 flock；mkdir 是原子的）----
# /sync-now 用 subprocess timeout=45 会直接 kill 脚本，日志里也有过 pull 卡 217 秒的记录，
# 而 StartInterval=600。无锁时两个实例同时动 git 仓 = 索引锁冲突 / 半途 rebase。
release_lock() { [ "$HOLD_LOCK" = "1" ] && rm -rf "$LOCK" 2>/dev/null; HOLD_LOCK=0; return 0; }
trap release_lock EXIT INT TERM

acquire_lock() {
    if mkdir "$LOCK" 2>/dev/null; then HOLD_LOCK=1; return 0; fi
    local age
    age=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || echo 0) ))
    if [ "$age" -gt 1200 ]; then
        log "发现陈旧锁（${age}s，上次多半被 /sync-now 的 45s 超时杀了），抢占"
        rm -rf "$LOCK" 2>/dev/null || true
        if mkdir "$LOCK" 2>/dev/null; then HOLD_LOCK=1; return 0; fi
    fi
    return 1
}

notify() {
    local title body
    title="$(printf '%s' "$1" | tr -d '"\\')"
    body="$(printf '%s' "$2" | tr -d '"\\')"
    osascript -e "display notification \"${body}\" with title \"${title}\" sound name \"Basso\"" >/dev/null 2>&1 || true
    log "NOTIFY: ${title} — ${body}"
}

# 统一收尾：写 status + 维护连续失败计数 + 达阈值通知 + 用**真实退出码**退出（R4）
finish() {
    local kind="$1" msg="$2" rc="$3" n=0
    echo "${kind}: ${msg}" > "$STATUS" 2>/dev/null || true
    [ -f "$FAILCOUNT" ] && n="$(cat "$FAILCOUNT" 2>/dev/null || echo 0)"
    case "$n" in ''|*[!0-9]*) n=0 ;; esac
    if [ "$kind" = "ok" ]; then
        [ "$n" -ge 3 ] && notify "金句同步已恢复" "连续失败 ${n} 次后恢复正常"
        rm -f "$FAILCOUNT" 2>/dev/null || true
    else
        n=$((n + 1)); echo "$n" > "$FAILCOUNT" 2>/dev/null || true
        # 连续 3 次（≈30 分钟）首告警；之后每 18 次（≈3 小时）提醒一次，避免刷屏
        if [ "$n" -eq 3 ] || { [ "$n" -gt 3 ] && [ "$((n % 18))" -eq 0 ]; }; then
            notify "金句同步失败 ${n} 次" "${msg}"
        fi
    fi
    log "finish: ${kind}: ${msg} (rc=${rc}, 连续失败=${n})"
    release_lock
    exit "$rc"
}

log "===== start @ $(hostname -s) ====="

if ! acquire_lock; then
    log "另一个同步实例正在跑，跳过本轮（不计失败）"
    echo "ok: 另一实例在跑，跳过" > "$STATUS" 2>/dev/null || true
    exit 0
fi

# 自愈身份
if [ -z "$(git config user.name)" ]; then git config user.name "quotes-app"; fi
if [ -z "$(git config user.email)" ]; then git config user.email "quotes-app@localhost"; fi

# 自愈注册 union 合并驱动
if [ -f json-merge.py ]; then
    git config merge.quotes-union.driver "$PY '$PWD/json-merge.py' %O %A %B" 2>/dev/null || true
    git config merge.quotes-union.name "quotes.json JSON-aware union merge" 2>/dev/null || true
fi

json_ok() { [ ! -f quotes.json ] || "$PY" -c "import json;json.load(open('quotes.json'))" 2>/dev/null; }
has_unmerged() { [ -n "$(git ls-files -u 2>/dev/null)" ]; }
has_autostash() { git stash list 2>/dev/null | grep -q autostash; }
ahead_count() { git rev-list --count origin/main..HEAD 2>/dev/null || echo 0; }

# ---- 开局自检：清理上次被 kill（/sync-now 45s 超时）留下的中间态 ----
# 没有这一步，半途 rebase 会让后续每一轮都在坏状态上打转。
if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
    log "发现上次遗留的未完成 rebase，abort 清理"
    gitq git rebase --abort || true
fi
if has_autostash; then
    log "发现遗留 autostash，先恢复本地改动再继续"
    if gitq git stash pop; then
        log "autostash 已恢复"
    else
        log "!! autostash 恢复失败 —— 本地改动仍在 git stash list 里"
        notify "金句同步：改动被暂存" "本地改动在 git stash 里未恢复，需手动 git stash pop"
        finish fail "遗留 autostash 未能恢复，需人工处理" 1
    fi
fi

# ---- 安全闸：quotes.json 必须合法 JSON ----
if ! json_ok; then
    log "quotes.json 非法 JSON，中止本次同步（不提交、不污染远端）"
    finish fail "quotes.json 非法 JSON" 1
fi

# ---- 第一步：本地 commit（不需要网络）----
# R5：旧版把 commit 放在 pull 之后，远端一坏本地就再不产生快照。
# 现在先落地本地版本历史，网络出问题最多是「没推上去」，绝不会「连快照都没有」。
# 副作用红利：工作区变干净后，下面的 rebase 基本不再依赖 autostash，R8 那条路几乎走不到。
commit_local() {
    if [ -z "$(git status --porcelain)" ]; then return 0; fi
    gitq git add -u || true
    # R6 附带发现：多 pathspec 一次 add，只要有一个文件不存在，git add 整条 fatal 且
    # 一个文件都不加（|| true 还会把错误吞掉）。所以逐个 add。
    for f in quotes.json json-merge.py sync.sh .gitattributes; do
        [ -e "$f" ] && { gitq git add "$f" || true; }
    done
    if gitq git commit -m "sync: $(date '+%Y-%m-%d %H:%M:%S') @ $(hostname -s)"; then
        log "本地已提交"
    else
        log "无新内容可提交（或只有被忽略的改动）"
    fi
}
commit_local

if ! git remote get-url origin > /dev/null 2>&1; then
    log "no remote, skip"; finish ok "无远端，跳过（本地已提交）" 0
fi

# ---------------- 网络层：硬超时 + 多线路兜底（N1/N2，见文件头 2026-09-25 段）----------------
FETCH_TIMEOUT=12
PUSH_TIMEOUT=20
ROUTE_FILE=".git/sync-route"
SSH_BASE="ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 -o StrictHostKeyChecking=accept-new"

# run_to <秒> <命令…>：带硬超时执行；超时返回 124
run_to() {
    local secs="$1"; shift
    if [ ! -x /usr/bin/perl ]; then "$@"; return $?; fi
    /usr/bin/perl -e '
        my $t = shift;
        my $pid = fork();
        die "fork: $!" unless defined $pid;
        if ($pid == 0) { setpgrp(0, 0); exec @ARGV or exit 127; }
        setpgrp($pid, $pid);
        $SIG{ALRM} = sub { kill "TERM", -$pid; sleep 2; kill "KILL", -$pid; waitpid($pid, 0); exit 124; };
        alarm $t;
        waitpid($pid, 0);
        alarm 0;
        exit(($? & 127) ? 128 + ($? & 127) : $? >> 8);
    ' "$secs" "$@"
}

# 系统 HTTPS 代理（Clash 等），没开就不试代理线路。
# 读 scutil 而不是 HTTPS_PROXY 环境变量：launchd 环境里没有代理变量（R3）
PROXY=""
if [ "$(scutil --proxy 2>/dev/null | awk '/HTTPSEnable/{print $3}')" = "1" ]; then
    PH="$(scutil --proxy | awk '/HTTPSProxy/{print $3}')"; PP="$(scutil --proxy | awk '/HTTPSPort/{print $3}')"
    if [ -n "$PH" ] && [ -n "$PP" ]; then PROXY="$PH:$PP"; fi
fi

# use_route <线路>：设置本次 git 的传输方式；该线路本机用不了返回 1
GIT_CFG=()
use_route() {
    GIT_CFG=()
    case "$1" in
        ssh22)        export GIT_SSH_COMMAND="$SSH_BASE" ;;
        ssh443)       export GIT_SSH_COMMAND="$SSH_BASE -o Hostname=ssh.github.com -p 443" ;;
        ssh443-proxy) [ -n "$PROXY" ] || return 1
                      export GIT_SSH_COMMAND="$SSH_BASE -o Hostname=ssh.github.com -p 443 -o ProxyCommand='nc -X connect -x $PROXY %h %p'" ;;
        https)        ;;
        https-proxy)  [ -n "$PROXY" ] || return 1; GIT_CFG=(-c "http.proxy=http://$PROXY") ;;
        *)            return 1 ;;
    esac
}

case "$(git remote get-url origin)" in
    https://*) ALL_ROUTES="https https-proxy" ;;
    *)         ALL_ROUTES="ssh22 ssh443 ssh443-proxy" ;;
esac
LAST="$(cat "$ROUTE_FILE" 2>/dev/null || true)"
ROUTES="${SYNC_ROUTES:-$LAST $ALL_ROUTES}"

# pull 之后无条件检查，不只在失败路径上查。
# R8：merge driver 退非 0 时 pull 仍返回 0，工作区被换成上游版本且不带冲突标记，
# JSON 闸看不出来 —— 本地新金句只剩孤儿 stash。必须靠 index 未合并态 + stash 残留来抓。
post_pull_guard() {
    if has_unmerged; then
        log "!! pull 后 index 存在未合并项（merge driver 可能失败），绝不 add/commit"
        gitq git rebase --abort || true
        gitq git merge --abort || true
        if has_autostash; then
            gitq git stash pop || log "!! autostash 未能恢复，本地改动在 git stash list 里"
        fi
        notify "金句同步：合并异常已拦截" "已中止提交，未污染远端，需人工查看"
        finish fail "合并异常（index 未合并），已拦截" 1
    fi
    if has_autostash; then
        log "!! pull 返回成功但 autostash 仍残留，说明回贴未完成"
        if gitq git stash pop; then
            log "autostash 已恢复"
        else
            notify "金句同步：改动被暂存" "本地改动在 git stash 里未恢复，需手动 git stash pop"
            finish fail "autostash 未能恢复，需人工处理" 1
        fi
    fi
    if ! json_ok; then
        log "!! pull 后 quotes.json 非法，中止（不提交、不推送）"
        finish fail "pull 后 quotes.json 非法 JSON" 1
    fi
}

# ---- 第二步：fetch（按线路、有超时）→ 本地 rebase（N3）----
ROUTE=""; TRIED=""
for r in $ROUTES; do
    case " $TRIED " in *" $r "*) continue ;; esac
    TRIED="$TRIED $r"
    use_route "$r" || continue
    if gitq run_to "$FETCH_TIMEOUT" git "${GIT_CFG[@]}" fetch -q origin main; then
        ROUTE="$r"; echo "$r" > "$ROUTE_FILE"; break
    else
        log "fetch via $r FAILED (rc=$?)"
    fi
done
if [ -z "$ROUTE" ]; then
    log "所有线路都连不上 GitHub（试过:$TRIED）"
    # 本地 commit 已在第一步完成，这里失败只是「没推上去」，本地历史是安全的
    finish fail "连不上 GitHub（本地已提交，等下次）" 1
fi
log "fetch ok via $ROUTE"

if gitq git rebase --autostash origin/main; then
    log "pull ok"
    post_pull_guard
else
    log "rebase 失败，放弃本轮"
    gitq git rebase --abort || true
    gitq git merge --abort || true
    if has_autostash; then
        gitq git stash pop || log "!! autostash 未能恢复，本地改动在 git stash list 里"
    fi
    finish fail "合并远端改动失败（本地已提交，等下次）" 1
fi

# ---- 第三步：push ----
# R6：门条件必须是「工作区脏 OR 本地领先远端」。旧版只看工作区脏不脏，
# 于是 push 一失败就永远卡住（commit 已让工作区变干净），状态还反写绿灯。
commit_local   # pull 之后可能又有 App 新写入，再收一次
AHEAD="$(ahead_count)"
if [ "$AHEAD" -gt 0 ]; then
    log "本地领先远端 ${AHEAD} 个提交，开始推送 via $ROUTE"
    if ! gitq run_to "$PUSH_TIMEOUT" git "${GIT_CFG[@]}" push -q origin main; then
        finish fail "推送失败（本地已提交，下次自动补推）" 1
    fi
    log "pushed $AHEAD commit(s) via $ROUTE"
    # 复核：推完必须真的不再领先，否则别报绿灯（复核的 fetch 同样要有超时）
    gitq run_to "$FETCH_TIMEOUT" git "${GIT_CFG[@]}" fetch -q origin main || true
    if [ "$(ahead_count)" -gt 0 ]; then
        finish fail "推送后本地仍领先远端，未真正同步" 1
    fi
    finish ok "已提交并推送" 0
else
    finish ok "已是最新" 0
fi
