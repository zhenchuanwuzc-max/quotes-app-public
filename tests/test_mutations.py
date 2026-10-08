#!/usr/bin/env python3
"""编辑 / 置顶 / 删除 三个改数据接口的测试。

每个操作都验：接口返回、列表里看得到效果、重启后还在（真落盘）、找不到的 id 回 404、
写入前把旧文件备份到（临时）备份目录。隔离方式同 test_smoke（见 _support.py）。
"""
import json
import os
import unittest

from _support import ServerTest, http, load_example

EX = load_example()["quotes"]          # 示例数据：第 1 条已置顶，第 2、3 条未置顶
UNKNOWN = "no-such-id-0000"


class MutationTest(ServerTest):

    def backups(self):
        return sorted(f for f in os.listdir(self.srv.backup_dir) if f.startswith("quotes-"))

    def backup_texts(self):
        """所有备份文件里出现过的正文集合（备份 = 写入前的旧版本）。"""
        texts = set()
        for f in self.backups():
            with open(os.path.join(self.srv.backup_dir, f), encoding="utf-8") as fh:
                texts.update(q["text"] for q in json.load(fh)["quotes"])
        return texts

    def patch(self, path, body=None, raw=None):
        st, _, text = http("PATCH", self.srv.base, path, body=body, raw=raw)
        return st, json.loads(text)

    def delete(self, qid):
        st, _, text = http("DELETE", self.srv.base, "/quotes/" + qid)
        return st, json.loads(text)

    # ================= 编辑 =================
    def test_edit_changes_text_source_use_case_and_persists(self):
        q = EX[1]
        st, d = self.patch("/quotes/" + q["id"],
                           {"text": "编辑后的正文", "source": "新出处", "use_case": "新用途"})
        self.assertEqual(st, 200, d)
        self.assertTrue(d["ok"])
        self.assertEqual(d["quote"]["text"], "编辑后的正文")

        got = self.find(q["id"])
        self.assertEqual((got["text"], got["source"], got["use_case"]), ("编辑后的正文", "新出处", "新用途"))
        self.assertEqual(got["created_at"], q["created_at"], "编辑不能改创建时间")
        self.assertEqual((got["pinned"], got["pinned_at"]), (q["pinned"], q["pinned_at"]), "编辑不能动置顶")
        self.assertTrue(got.get("updated_at"), "编辑要写 updated_at，跨机合并靠它判断谁新")
        self.assertNotEqual(got["updated_at"], q["created_at"])
        self.assertEqual(len(self.list_all()), self.srv.seed_count)

        self.assertIn(q["text"], self.backup_texts(), "编辑前的版本应进备份目录")

        self.srv.restart()
        got = self.find(q["id"])
        self.assertEqual(got["text"], "编辑后的正文")
        self.assertEqual(self.srv.read_file()["quotes"][1]["text"], "编辑后的正文")

    def test_edit_trims_and_rejects_empty_text(self):
        q = EX[2]
        st, _ = self.patch("/quotes/" + q["id"], {"text": "   ", "source": "x"})
        self.assertEqual(st, 400)
        self.assertEqual(self.find(q["id"])["text"], q["text"])
        st, d = self.patch("/quotes/" + q["id"], {"text": "  两边有空格  "})
        self.assertEqual(st, 200, d)
        self.assertEqual(self.find(q["id"])["text"], "两边有空格")

    def test_edit_with_stale_version_is_rejected(self):
        # 防「另一台设备已经改过，这边拿旧版本覆盖」：expected_updated_at 对不上 → 409，内容不动
        q = EX[2]
        st, d = self.patch("/quotes/" + q["id"], {"text": "第一次", "expected_updated_at": q["created_at"]})
        self.assertEqual(st, 200, d)
        first_version = d["quote"]["updated_at"]

        st, d = self.patch("/quotes/" + q["id"], {"text": "拿旧版本改", "expected_updated_at": q["created_at"]})
        self.assertEqual(st, 409)
        self.assertEqual(d["current_updated_at"], first_version)
        self.assertEqual(self.find(q["id"])["text"], "第一次")

        st, d = self.patch("/quotes/" + q["id"], {"text": "拿新版本改", "expected_updated_at": first_version})
        self.assertEqual(st, 200, d)
        self.assertEqual(self.find(q["id"])["text"], "拿新版本改")

    def test_edit_unknown_id_404(self):
        st, _ = self.patch("/quotes/" + UNKNOWN, {"text": "随便"})
        self.assertEqual(st, 404)
        self.assertEqual(self.backups(), [], "没改成就不该写备份")

    def test_edit_invalid_json_400(self):
        st, _ = self.patch("/quotes/" + EX[1]["id"], raw=b"{not json")
        self.assertEqual(st, 400)
        self.assertEqual(self.find(EX[1]["id"])["text"], EX[1]["text"])

    # ================= 置顶 =================
    def test_pin_moves_quote_to_top_and_persists(self):
        q = EX[2]
        st, d = self.patch("/quotes/%s/pin" % q["id"], {"pinned": True})
        self.assertEqual(st, 200, d)
        self.assertTrue(d["quote"]["pinned"])
        self.assertTrue(d["quote"]["pinned_at"])

        quotes = self.list_all()
        pinned_ids = [x["id"] for x in quotes if x["pinned"]]
        self.assertEqual(set(pinned_ids), {EX[0]["id"], q["id"]})
        self.assertEqual([x["pinned"] for x in quotes], sorted([x["pinned"] for x in quotes], reverse=True),
                         "置顶的应排在最前")
        self.assertEqual(self.find(q["id"])["text"], q["text"], "置顶不能动正文")
        self.assertTrue(self.backups(), "置顶前应写备份")

        self.srv.restart()
        self.assertTrue(self.find(q["id"])["pinned"])

    def test_unpin_persists_and_refreshes_pinned_at(self):
        # 取消置顶也要刷新 pinned_at，否则跨机合并时会被另一台的旧「置顶」盖回去
        q = EX[0]
        st, d = self.patch("/quotes/%s/pin" % q["id"], {"pinned": False})
        self.assertEqual(st, 200, d)
        self.assertFalse(d["quote"]["pinned"])
        self.assertNotEqual(d["quote"]["pinned_at"], q["pinned_at"])
        self.assertFalse(self.find(q["id"])["pinned"])
        self.srv.restart()
        self.assertFalse(self.find(q["id"])["pinned"])
        self.assertEqual([x for x in self.list_all() if x["pinned"]], [])

    def test_pin_unknown_id_404(self):
        st, _ = self.patch("/quotes/%s/pin" % UNKNOWN, {"pinned": True})
        self.assertEqual(st, 404)

    # ================= 删除 =================
    def test_delete_removes_quote_and_persists(self):
        q = EX[1]
        st, d = self.delete(q["id"])
        self.assertEqual(st, 200, d)
        self.assertTrue(d["ok"])

        ids = [x["id"] for x in self.list_all()]
        self.assertNotIn(q["id"], ids)
        self.assertEqual(len(ids), self.srv.seed_count - 1)
        self.assertEqual(self.get("/health")[1]["total"], self.srv.seed_count - 1)
        self.assertIn(q["text"], self.backup_texts(), "删之前的版本必须进备份——删除只能靠它找回")

        self.srv.restart()
        self.assertNotIn(q["id"], [x["id"] for x in self.list_all()])
        self.assertNotIn(q["id"], [x["id"] for x in self.srv.read_file()["quotes"]])

        st, _ = self.delete(q["id"])
        self.assertEqual(st, 404, "删第二次应 404")

    def test_delete_unknown_id_404_and_touches_nothing(self):
        st, _ = self.delete(UNKNOWN)
        self.assertEqual(st, 404)
        self.assertEqual(len(self.list_all()), self.srv.seed_count)
        self.assertEqual(self.backups(), [])

    def test_added_quote_can_be_edited_pinned_and_deleted(self):
        st, _, body = http("POST", self.srv.base, "/quotes/add", {"text": "新增后全流程"})
        self.assertEqual(st, 200, body)
        qid = json.loads(body)["quote"]["id"]
        self.assertEqual(self.patch("/quotes/" + qid, {"text": "改过一次"})[0], 200)
        self.assertEqual(self.patch("/quotes/%s/pin" % qid, {"pinned": True})[0], 200)
        got = self.find(qid)
        self.assertEqual((got["text"], got["pinned"]), ("改过一次", True))
        self.assertEqual(self.list_all()[0]["id"], qid, "最新置顶的排第一")
        self.assertEqual(self.delete(qid)[0], 200)
        self.assertIsNone(self.find(qid))
        self.assertEqual(len(self.list_all()), self.srv.seed_count)


if __name__ == "__main__":
    unittest.main(verbosity=2)
