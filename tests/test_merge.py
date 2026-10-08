#!/usr/bin/env python3
"""json-merge.py（quotes.json 的 git 合并驱动）的测试 —— 多机同步不丢数据的最后一道防线。

两层：
- MergeDriverTest：按 git 的真实调用方式跑脚本（argv = %O %A %B，结果写回 %A，看退出码），
  覆盖 增 / 改 / 置顶 / 删 / 重复 id / 坏输入。
- GitSyncTest：一个本地 bare 仓当远端 + 两个 clone 当两台电脑，注册同一个合并驱动，
  按 sync.sh 的顺序（commit → fetch → rebase --autostash → push）真跑 git。不联网。

数据全是合成的（公开仓，不放真实金句）。驱动日志用 QUOTES_MERGE_LOG 指到临时目录，不写 /tmp/quotes-merge.log。
"""
import json
import os
import subprocess
import sys
import unittest

from _support import MERGE_PY, TimeLimitedTest, load_example

T0 = "2026-01-01T09:00:00+08:00"


def ts(day: int, hour: int = 10) -> str:
    return "2026-01-%02dT%02d:00:00+08:00" % (day, hour)


def quote(qid, text=None, *, source="", use_case="", created=T0, updated=None,
          pinned=False, pinned_at=None):
    return {
        "id": qid,
        "text": text if text is not None else "synthetic quote " + qid,
        "source": source,
        "use_case": use_case,
        "created_at": created,
        "updated_at": updated or created,
        "pinned": pinned,
        "pinned_at": pinned_at,
    }


def doc(*quotes, updated=T0):
    return {"version": "0.1.0", "updated": updated, "quotes": [dict(q) for q in quotes]}


def by_id(d):
    return {q["id"]: q for q in d["quotes"]}


class MergeDriverTest(TimeLimitedTest):

    def run_driver(self, base, ours, theirs):
        """base/ours/theirs：dict（写成 JSON）、str（原样写入）或 None（空文件，git 无共同祖先时就是这样）。
        返回 (退出码, 合并后 %A 的原文)。"""
        paths = []
        for name, content in (("O", base), ("A", ours), ("B", theirs)):
            p = os.path.join(self.tmp, name)
            with open(p, "w", encoding="utf-8") as f:
                if isinstance(content, dict):
                    json.dump(content, f, ensure_ascii=False, indent=2)
                elif content is not None:
                    f.write(content)
            paths.append(p)
        env = dict(os.environ, QUOTES_MERGE_LOG=os.path.join(self.tmp, "merge.log"))
        r = subprocess.run([sys.executable, MERGE_PY] + paths, env=env,
                           capture_output=True, text=True, timeout=20)
        with open(paths[1], encoding="utf-8") as f:
            return r.returncode, f.read()

    def merge(self, base, ours, theirs):
        """期望合并成功：断言退出码 0、无冲突标记、合法 JSON，返回结果 dict。"""
        rc, out = self.run_driver(base, ours, theirs)
        self.assertEqual(rc, 0, "合并驱动应成功，输出：\n" + out)
        for mark in ("<<<<<<<", ">>>>>>>", "\n=======\n"):
            self.assertNotIn(mark, out)
        return json.loads(out)

    def merge_both_ways(self, base, a, b):
        """两台机器各自合并（A 合 B、B 合 A）必须得到同样的金句集合，否则会来回打架。"""
        ab = self.merge(base, a, b)
        ba = self.merge(base, b, a)
        self.assertEqual(by_id(ab), by_id(ba), "两个方向合并结果不一致")
        return ab

    # ---- 新增 ----
    def test_both_machines_add_different_quotes_all_kept(self):
        base = doc(quote("s1"), quote("s2"))
        a = doc(quote("s1"), quote("s2"), quote("a1", created=ts(2)))
        b = doc(quote("s1"), quote("s2"), quote("b1", created=ts(3)), quote("b2", created=ts(4)))
        m = self.merge_both_ways(base, a, b)
        self.assertEqual(sorted(by_id(m)), ["a1", "b1", "b2", "s1", "s2"])
        self.assertEqual(len(m["quotes"]), 5)

    def test_add_with_no_common_base(self):
        # 两台机器各自从零建库后第一次合并：git 给的 %O 是空文件
        m = self.merge_both_ways(None, doc(quote("a1")), doc(quote("b1")))
        self.assertEqual(sorted(by_id(m)), ["a1", "b1"])

    def test_example_seed_round_trip(self):
        # 用仓库自带的示例数据走一遍：一边新增，另一边原样 → 结果 = 示例 + 新增，字段原样保留
        seed = load_example()
        a = json.loads(json.dumps(seed))
        a["quotes"].append(quote("a1", created=ts(9)))
        m = self.merge_both_ways(seed, a, seed)
        self.assertEqual(len(m["quotes"]), len(seed["quotes"]) + 1)
        for q in seed["quotes"]:
            self.assertEqual(by_id(m)[q["id"]], q)

    # ---- 重复 id ----
    def test_same_quote_on_both_sides_not_doubled(self):
        same = quote("x1", created=ts(2))
        m = self.merge_both_ways(doc(), doc(same), doc(same))
        self.assertEqual([q["id"] for q in m["quotes"]], ["x1"])

    def test_duplicate_id_inside_one_file_not_doubled(self):
        q = quote("x1", created=ts(2))
        m = self.merge(doc(), doc(q, q), doc(quote("b1")))
        ids = [x["id"] for x in m["quotes"]]
        self.assertEqual(ids.count("x1"), 1)
        self.assertEqual(sorted(ids), ["b1", "x1"])

    # ---- 编辑 ----
    def test_edit_on_one_side_wins_over_unchanged(self):
        base = doc(quote("s1", "old text"))
        edited = doc(quote("s1", "new text", source="new src", updated=ts(5)))
        m = self.merge_both_ways(base, edited, base)
        self.assertEqual(by_id(m)["s1"]["text"], "new text")
        self.assertEqual(by_id(m)["s1"]["source"], "new src")

    def test_edit_on_both_sides_later_edit_wins_as_a_whole(self):
        base = doc(quote("s1", "old", source="old src"))
        early = doc(quote("s1", "early text", source="early src", use_case="u1", updated=ts(5)))
        late = doc(quote("s1", "late text", source="old src", use_case="", updated=ts(6)))
        m = self.merge_both_ways(base, early, late)
        got = by_id(m)["s1"]
        # 整组取晚的那次编辑，绝不把两次编辑的字段拼在一起
        self.assertEqual((got["text"], got["source"], got["use_case"], got["updated_at"]),
                         ("late text", "old src", "", ts(6)))

    def test_edit_and_pin_on_different_machines_both_survive(self):
        base = doc(quote("s1", "old"))
        edited = doc(quote("s1", "edited", updated=ts(5)))
        pinned = doc(quote("s1", "old", pinned=True, pinned_at=ts(6)))
        m = self.merge_both_ways(base, edited, pinned)
        got = by_id(m)["s1"]
        self.assertEqual(got["text"], "edited")
        self.assertTrue(got["pinned"])
        self.assertEqual(got["pinned_at"], ts(6))

    # ---- 置顶 ----
    def test_later_unpin_beats_earlier_pin(self):
        base = doc(quote("s1", pinned=True, pinned_at=ts(2)))
        unpinned = doc(quote("s1", pinned=False, pinned_at=ts(5)))
        m = self.merge_both_ways(base, base, unpinned)
        self.assertFalse(by_id(m)["s1"]["pinned"])

    def test_later_pin_beats_earlier_unpin(self):
        base = doc(quote("s1"))
        unpinned = doc(quote("s1", pinned=False, pinned_at=ts(3)))
        pinned = doc(quote("s1", pinned=True, pinned_at=ts(4)))
        m = self.merge_both_ways(base, unpinned, pinned)
        self.assertTrue(by_id(m)["s1"]["pinned"])

    # ---- 删除 ----
    def test_delete_on_one_side_propagates(self):
        base = doc(quote("s1"), quote("s2"))
        deleted = doc(quote("s2"))
        m = self.merge_both_ways(base, deleted, base)
        self.assertEqual(sorted(by_id(m)), ["s2"])

    def test_delete_on_both_sides(self):
        base = doc(quote("s1"), quote("s2"))
        m = self.merge_both_ways(base, doc(quote("s2")), doc(quote("s2")))
        self.assertEqual(sorted(by_id(m)), ["s2"])

    def test_delete_vs_edit_keeps_the_edit(self):
        base = doc(quote("s1", "old"), quote("s2"))
        deleted = doc(quote("s2"))
        edited = doc(quote("s1", "edited", updated=ts(5)), quote("s2"))
        m = self.merge_both_ways(base, deleted, edited)
        self.assertEqual(by_id(m)["s1"]["text"], "edited")

    def test_delete_vs_pin_keeps_the_quote(self):
        base = doc(quote("s1"), quote("s2"))
        deleted = doc(quote("s2"))
        pinned = doc(quote("s1", pinned=True, pinned_at=ts(5)), quote("s2"))
        m = self.merge_both_ways(base, deleted, pinned)
        self.assertTrue(by_id(m)["s1"]["pinned"])

    def test_delete_plus_add_on_other_side(self):
        base = doc(quote("s1"), quote("s2"))
        a = doc(quote("s2"))                               # 删 s1
        b = doc(quote("s1"), quote("s2"), quote("b1"))     # 加 b1
        m = self.merge_both_ways(base, a, b)
        self.assertEqual(sorted(by_id(m)), ["b1", "s2"])

    def test_unreadable_base_never_deletes(self):
        # 共同祖先读不出来时，宁可让删掉的复活，也不能误删
        a = doc(quote("s2"))
        b = doc(quote("s1"), quote("s2"))
        m = self.merge(b"not json".decode(), a, b)
        self.assertEqual(sorted(by_id(m)), ["s1", "s2"])

    # ---- 坏输入：必须响亮地失败，不许悄悄丢掉一整边 ----
    def assert_refuses(self, base, ours, theirs):
        before = ours if isinstance(ours, str) else json.dumps(ours, ensure_ascii=False, indent=2)
        rc, out = self.run_driver(base, ours, theirs)
        self.assertNotEqual(rc, 0, "坏输入时合并驱动必须退非 0（让 git 标记未合并、sync.sh 拦截并通知），"
                                   "实际退 0，结果：\n" + out[:400])
        self.assertEqual(out, before, "失败时不许改写 %A")

    def test_ours_unparseable_fails_instead_of_dropping_ours(self):
        base = doc(quote("s1"))
        self.assert_refuses(base, '{"quotes": [ {"id": "a1", "text": "trunc', doc(quote("s1"), quote("b1")))

    def test_theirs_unparseable_fails_instead_of_dropping_theirs(self):
        base = doc(quote("s1"))
        self.assert_refuses(base, doc(quote("s1"), quote("a1")), '{"quotes": [ {"id": "b1"')

    def test_both_unparseable_fails(self):
        self.assert_refuses(doc(), "garbage", "more garbage")

    def test_side_without_quotes_list_fails_instead_of_deleting_everything(self):
        # 一边是 {}（或 quotes 不是数组）：旧逻辑会把它当成「全删了」，把另一边所有没改过的金句都删掉
        base = doc(quote("s1"), quote("s2"))
        self.assert_refuses(base, "{}", base)
        self.assert_refuses(base, base, '{"quotes": {"s1": 1}}')

    def test_quote_without_id_fails_instead_of_vanishing(self):
        base = doc(quote("s1"))
        no_id = quote("tmp", created=ts(3))
        del no_id["id"]
        self.assert_refuses(base, doc(quote("s1"), no_id), base)

    def test_quote_that_is_not_an_object_fails(self):
        base = doc(quote("s1"))
        self.assert_refuses(base, base, '{"quotes": ["just a string"]}')


# ======================================================================
# 端到端：两台「电脑」+ 一个本地远端，真跑 git + 合并驱动
# ======================================================================

class GitSyncTest(TimeLimitedTest):
    time_limit = 60

    def setUp(self):
        super().setUp()
        self.env = dict(
            os.environ,
            HOME=self.tmp,                      # 不读 Ocean 的 ~/.gitconfig（签名、hooks 等）
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            QUOTES_MERGE_LOG=os.path.join(self.tmp, "merge.log"),
        )
        self.remote = os.path.join(self.tmp, "remote.git")
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", self.remote)

        seed = os.path.join(self.tmp, "seed")
        self.git(self.tmp, "clone", "-q", self.remote, seed)
        self.configure(seed)
        with open(os.path.join(seed, ".gitattributes"), "w") as f:
            f.write("quotes.json merge=quotes-union\n")
        self.write(seed, load_example())
        self.git(seed, "add", ".")
        self.git(seed, "commit", "-q", "-m", "seed")
        self.git(seed, "push", "-q", "origin", "main")

        self.a = self.clone("machine-a")
        self.b = self.clone("machine-b")

    # ---- helpers ----
    def git(self, cwd, *args, check=True):
        r = subprocess.run(["git"] + list(args), cwd=cwd, env=self.env,
                           capture_output=True, text=True, timeout=20)
        if check and r.returncode != 0:
            raise AssertionError("git %s 失败：\n%s%s" % (" ".join(args), r.stdout, r.stderr))
        return r

    def configure(self, repo):
        self.git(repo, "config", "user.name", "test")
        self.git(repo, "config", "user.email", "test@localhost")
        self.git(repo, "config", "commit.gpgsign", "false")
        # 与 sync.sh / install.sh 注册方式相同，只是指向代码仓里这份驱动
        self.git(repo, "config", "merge.quotes-union.driver",
                 "'%s' '%s' %%O %%A %%B" % (sys.executable, MERGE_PY))

    def clone(self, name):
        path = os.path.join(self.tmp, name)
        self.git(self.tmp, "clone", "-q", self.remote, path)
        self.configure(path)
        return path

    def read(self, repo):
        with open(os.path.join(repo, "quotes.json"), encoding="utf-8") as f:
            return json.load(f)

    def write(self, repo, d):
        # 与 server._atomic_write 相同的写法：indent=2、不转义中文
        with open(os.path.join(repo, "quotes.json"), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)

    def change(self, repo, fn, msg):
        d = self.read(repo)
        fn(d)
        self.write(repo, d)
        self.git(repo, "commit", "-q", "-am", msg)

    def sync(self, repo):
        """sync.sh 的核心顺序：（已本地 commit）→ fetch → rebase --autostash → push。"""
        self.git(repo, "fetch", "-q", "origin", "main")
        r = self.git(repo, "rebase", "--autostash", "origin/main", check=False)
        unmerged = self.git(repo, "ls-files", "-u").stdout.strip()
        if r.returncode != 0 or unmerged:
            return False
        self.git(repo, "push", "-q", "origin", "main")
        return True

    def assert_clean_json(self, repo):
        with open(os.path.join(repo, "quotes.json"), encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("<<<<<<<", raw)
        json.loads(raw)

    # ---- scenarios ----
    def test_two_machines_add_concurrently_both_kept_everywhere(self):
        seed_n = len(load_example()["quotes"])
        self.change(self.a, lambda d: d["quotes"].append(quote("a1", "机器 A 新增", created=ts(2))), "a add")
        self.change(self.b, lambda d: d["quotes"].append(quote("b1", "机器 B 新增", created=ts(3))), "b add")
        self.assertTrue(self.sync(self.a))
        self.assertTrue(self.sync(self.b), "B 合并 A 的改动失败")
        self.assertTrue(self.sync(self.a))
        for repo in (self.a, self.b):
            self.assert_clean_json(repo)
            ids = [q["id"] for q in self.read(repo)["quotes"]]
            self.assertEqual(len(ids), seed_n + 2)
            self.assertEqual(len(set(ids)), len(ids))
            self.assertIn("a1", ids)
            self.assertIn("b1", ids)

    def test_delete_pin_edit_add_across_machines(self):
        ex = [q["id"] for q in load_example()["quotes"]]

        def on_a(d):
            d["quotes"] = [q for q in d["quotes"] if q["id"] != ex[1]]       # 删第 2 条
            for q in d["quotes"]:
                if q["id"] == ex[2]:
                    q.update(text="A 改过的正文", updated_at=ts(5))           # 改第 3 条

        def on_b(d):
            for q in d["quotes"]:
                if q["id"] == ex[2]:
                    q.update(pinned=True, pinned_at=ts(6))                    # 置顶第 3 条
                if q["id"] == ex[0]:
                    q.update(pinned=False, pinned_at=ts(6))                   # 取消置顶第 1 条
            d["quotes"].append(quote("b1", created=ts(7)))                    # 新增

        self.change(self.a, on_a, "a edits")
        self.change(self.b, on_b, "b edits")
        self.assertTrue(self.sync(self.a))
        self.assertTrue(self.sync(self.b))
        self.assertTrue(self.sync(self.a))
        for repo in (self.a, self.b):
            self.assert_clean_json(repo)
            got = by_id(self.read(repo))
            self.assertNotIn(ex[1], got)
            self.assertEqual(got[ex[2]]["text"], "A 改过的正文")
            self.assertTrue(got[ex[2]]["pinned"])
            self.assertFalse(got[ex[0]]["pinned"])
            self.assertIn("b1", got)
        self.assertEqual(self.read(self.a), self.read(self.b))

    def test_uncommitted_edit_survives_rebase_autostash(self):
        # App 刚写完、sync.sh 还没 commit 时远端有新内容：--autostash 后本地改动必须还在
        self.change(self.a, lambda d: d["quotes"].append(quote("a1", created=ts(2))), "a add")
        self.assertTrue(self.sync(self.a))
        d = self.read(self.b)
        d["quotes"].append(quote("b-wip", created=ts(3)))
        self.write(self.b, d)
        self.git(self.b, "fetch", "-q", "origin", "main")
        self.git(self.b, "rebase", "--autostash", "origin/main")
        self.assertEqual(self.git(self.b, "stash", "list").stdout.strip(), "")
        self.assertEqual(sorted(i for i in by_id(self.read(self.b)) if not i.startswith("example")),
                         ["a1", "b-wip"])

    def test_broken_remote_file_is_flagged_unmerged_not_silently_resolved(self):
        # 远端被别的机器推了个坏文件：驱动必须失败 → git 标「未合并」→ sync.sh 的 post_pull_guard 拦截
        with open(os.path.join(self.a, "quotes.json"), "w") as f:
            f.write("{}")
        self.git(self.a, "commit", "-q", "-am", "broken")
        self.git(self.a, "push", "-q", "origin", "main")
        self.change(self.b, lambda d: d["quotes"].append(quote("b1", created=ts(3))), "b add")
        self.assertFalse(self.sync(self.b), "坏的远端文件不应被合并通过")
        self.git(self.b, "rebase", "--abort")
        self.assertIn("b1", by_id(self.read(self.b)), "中止后 B 的本地新增必须还在")


if __name__ == "__main__":
    unittest.main(verbosity=2)
