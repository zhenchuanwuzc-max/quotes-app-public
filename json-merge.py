#!/usr/bin/env python3
"""
git merge driver —— quotes.json 的 JSON-aware union 合并

改自 ~/daily-todo/json-merge.py（同款范式）。关键差异：
  - tasks → quotes
  - 同 id 合并：**两个解耦的原子组各自 LWW，再组装**（推翻原 append-only）
      * 正文原子组 {text,source,use_case,updated_at}：按 updated_at(兜底 created_at) 晚的整组取
        —— 🔴 整组覆盖，禁逐字段拼（A改text+B改source 逐字段拼会产出错乱数据）
      * pin 原子组 {pinned,pinned_at}：独立按 pinned_at 晚的取
      * updated_at 相等时按正文内容字典序 tie-break（保双机反向 merge 收敛同状态）
  - 删除传播：完全照抄 daily-todo 的 base(%O) 三方 diff，**无 tombstones**
    （编辑过的条目 updated_at 变 → o!=bse → 正确保留；前提：绝不批量刷历史 updated_at）

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


def _content_recency(q):
    """正文新近度：updated_at 优先，历史条目无此字段 → 兜底 created_at。永远返回 str。"""
    return str(q.get("updated_at") or q.get("created_at") or "")


def merge_same(a, b):
    """同 id 两条合并：两个解耦原子组各自 LWW 后组装。
    - 正文原子组 {text,source,use_case,updated_at}：按 _content_recency 晚的整组取
      （updated_at 相等 → 按 id 字典序 tie-break，保双机反向 merge 收敛同状态）
    - pin 原子组 {pinned,pinned_at}：独立按 pinned_at 晚的取
    解耦后：改正文不覆盖另一机刚 pin 的状态。所有比较前 or "" 兜底防 None 异类型崩。"""
    # 正文原子组：整组取晚侧（禁逐字段拼）
    ra, rb = _content_recency(a), _content_recency(b)
    if ra != rb:
        content_src = a if ra > rb else b
    else:
        # tie：updated_at 相等（同 id，id 无法区分），按正文内容字典序确定取一侧
        # —— 保证双机反向 merge 收敛到同一结果（交换律），破解 >= 非确定性
        ka = (a.get("text") or "", a.get("source") or "", a.get("use_case") or "")
        kb = (b.get("text") or "", b.get("source") or "", b.get("use_case") or "")
        content_src = a if ka <= kb else b
    # pin 原子组：独立按 pinned_at LWW
    pa, pb = (a.get("pinned_at") or ""), (b.get("pinned_at") or "")
    pin_src = a if pa >= pb else b
    # 组装：以正文晚侧为基底，叠加 pin 晚侧的 pin 字段
    merged = dict(content_src)
    merged["pinned"] = pin_src.get("pinned", False)
    merged["pinned_at"] = pin_src.get("pinned_at")
    return merged


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
