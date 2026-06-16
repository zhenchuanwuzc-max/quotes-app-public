#!/usr/bin/env python3
"""
git merge driver —— quotes.json 的 JSON-aware union 合并

改自 ~/the reference app/json-merge.py（同款范式）。关键差异：
  - tasks → quotes
  - 较新判定 _recency 用 created_at（金句正文 append-only，不可编辑）
  - 同 id 合并：done-OR-logic → **pinned 走 pinned_at LWW**（晚操作胜出，破 un-pin trap）
  - 删除传播：完全照抄 the reference app 的 base(%O) 三方 diff，**无 tombstones**

注册（每台设备本地配置，sync.sh 会自愈注册）：
    git config merge.quotes-union.driver "python3 <此脚本> %O %A %B"
.gitattributes:
    quotes.json merge=quotes-union

语义（专为"单人多设备金句库"设计）：
  - 按 quote id 取并集：不同 id 两边都保留（add/add 撞位 → 不产生冲突标记）
  - 相同 id：金句正文不可改，唯一可变字段 = pinned；按 pinned_at 取晚的那条（LWW）
  - 删除传播：用 base(%O) 识别——某 quote 在 base+本侧存在、对侧已删且本侧未改 → 视为对侧删除，丢弃
    （删 vs 改 pin 冲突 → 保留被改 pin 的那条，偏向不丢）
  - 兜底：任一侧能解析就产出合法 JSON；两侧都解析不了才退非 0（让 git 退回标记 + 记日志）

目标铁律：合并结果永远是合法 JSON、无冲突标记、不丢"新增"的金句。
"""
import sys
import json
import re

_MARK = re.compile(r'^(<<<<<<<|=======|>>>>>>>|\|\|\|\|\|\|\|)')
_LOG = "/tmp/quotes-merge.log"


def _log(msg):
    try:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def load(path):
    """读 JSON；失败时先剥可能存在的冲突标记行再试。返回 (dict_or_None, ok)。"""
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except Exception:
        return None, False
    cleaned = "\n".join(l for l in raw.splitlines() if not _MARK.match(l))
    for txt in (raw, cleaned):
        try:
            d = json.loads(txt)
            if isinstance(d, dict):
                return d, True
        except Exception:
            continue
    return None, False


def quote_map(d):
    return {q.get("id"): q for q in (d.get("quotes") or []) if q.get("id")}


def merge_same(a, b):
    """同 id 两条合并：金句正文不可改，按 pinned_at 取晚的（LWW）。
    都为 null（从没 pin 过）→ 取任一（其余字段相同）。"""
    pa = a.get("pinned_at") or ""
    pb = b.get("pinned_at") or ""
    return a if pa >= pb else b


def union(ours, theirs, base):
    o_map, t_map = quote_map(ours), quote_map(theirs)
    b_map = quote_map(base) if isinstance(base, dict) else {}
    # 保持顺序：先 theirs 顺序，再 ours 独有
    order = list(dict.fromkeys(
        [q.get("id") for q in (theirs.get("quotes") or [])] +
        [q.get("id") for q in (ours.get("quotes") or [])]
    ))
    merged = []
    for i in order:
        if not i:
            continue
        o, t, bse = o_map.get(i), t_map.get(i), b_map.get(i)
        if o and t:
            merged.append(merge_same(o, t))
        elif o and not t:
            # theirs 没有：是 theirs 删了，还是 ours 新增？
            if bse is not None and o == bse:
                continue  # base 有 + ours 未改 → theirs 删除 → 丢弃
            merged.append(o)  # ours 新增 / ours 改过(改 pin vs 删) → 保留
        elif t and not o:
            if bse is not None and t == bse:
                continue  # ours 删除 → 丢弃
            merged.append(t)
    result = dict(theirs)
    result.update({k: v for k, v in ours.items() if k != "quotes"})
    result["updated"] = (max(str(ours.get("updated", "")),
                             str(theirs.get("updated", "")))
                         or theirs.get("updated"))
    result["quotes"] = merged
    return result


def main():
    if len(sys.argv) < 4:
        _log("[quotes-merge] bad args")
        return 1
    O, A, B = sys.argv[1], sys.argv[2], sys.argv[3]
    ours, ok_a = load(A)
    theirs, ok_b = load(B)
    base, _ = load(O)

    if not ok_a and not ok_b:
        _log("[quotes-merge] both sides unparseable -> fallback to git markers")
        return 1
    if ok_a and not ok_b:
        result = ours
    elif ok_b and not ok_a:
        result = theirs
    else:
        result = union(ours, theirs, base)

    try:
        with open(A, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _log(f"[quotes-merge] write failed: {e}")
        return 1
    _log(f"[quotes-merge] union ok -> {len(result.get('quotes', []))} quotes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
