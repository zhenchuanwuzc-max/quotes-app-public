#!/usr/bin/env python3
"""quotes-app 主流程冒烟测试（只测主流程，不追求全覆盖）。

隔离措施（全部在每个测试里强制）：
- 数据目录 / 备份目录 / 壁纸目录 / HOME 全部指向临时目录，绝不碰 ~/quotes-data 与 _backups
- 随机空闲端口起 server.py 子进程，绝不碰线上 8767
- 临时数据目录里不放 sync.sh → server 的 schedule_sync() 直接 return，不做 git/网络
- HTTP 客户端禁用系统代理
- 每个测试 30 秒硬上限（SIGALRM），结束必杀子进程并删临时目录

运行：python3 -m unittest discover -s tests -v
"""
import json
import os
import time
import unittest
import urllib.parse

from _support import ServerTest, http


class SmokeTest(ServerTest):
    # ---- 1. 首页 ----
    def test_01_home_page_is_html(self):
        st, headers, body = http("GET", self.srv.base, "/")
        self.assertEqual(st, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("<html", body.lower())
        self.assertNotIn("index.html not loaded", body)

    # ---- 2. 读列表 ----
    def test_02_list_reads_seed_data(self):
        st, _, body = http("GET", self.srv.base, "/quotes")
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertTrue(d["ok"])
        self.assertEqual(d["total"], self.srv.seed_count)
        self.assertEqual(len(d["quotes"]), self.srv.seed_count)
        st, _, body = http("GET", self.srv.base, "/health")
        self.assertEqual(json.loads(body)["total"], self.srv.seed_count)

    # ---- 3. 新增 + 再读 ----
    def test_03_add_then_visible_in_list(self):
        st, _, body = http("POST", self.srv.base, "/quotes/add",
                           {"text": "冒烟测试金句 alpha", "source": "测试来源", "use_case": "smoke"})
        self.assertEqual(st, 200, body)
        d = json.loads(body)
        self.assertTrue(d["ok"])
        qid = d["quote"]["id"]
        self.assertEqual(d["total"], self.srv.seed_count + 1)

        st, _, body = http("GET", self.srv.base, "/quotes?limit=100")
        d = json.loads(body)
        self.assertEqual(d["total"], self.srv.seed_count + 1)
        added = [q for q in d["quotes"] if q["id"] == qid]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["text"], "冒烟测试金句 alpha")
        self.assertEqual(added[0]["source"], "测试来源")

    def test_04_add_empty_text_rejected(self):
        st, _, _ = http("POST", self.srv.base, "/quotes/add", {"text": "   "})
        self.assertEqual(st, 400)
        st, _, body = http("GET", self.srv.base, "/quotes")
        self.assertEqual(json.loads(body)["total"], self.srv.seed_count)

    # ---- 5. 持久化：重启后数据仍在 ----
    def test_05_data_survives_restart(self):
        st, _, body = http("POST", self.srv.base, "/quotes/add",
                           {"text": "重启后还在 beta", "source": "persist"})
        self.assertEqual(st, 200, body)
        qid = json.loads(body)["quote"]["id"]

        self.srv.restart()

        st, _, body = http("GET", self.srv.base, "/quotes?limit=100")
        d = json.loads(body)
        self.assertEqual(d["total"], self.srv.seed_count + 1)
        self.assertIn(qid, [q["id"] for q in d["quotes"]])
        # 文件也确实落在临时数据目录
        with open(os.path.join(self.srv.data_dir, "quotes.json"), encoding="utf-8") as f:
            self.assertIn(qid, [q["id"] for q in json.load(f)["quotes"]])

    # ---- 6. 搜索 ----
    def test_06_search_hits_and_misses(self):
        # 命中种子数据（来源字段）
        st, _, body = http("GET", self.srv.base, "/quotes?q=" + urllib.parse.quote("Drucker"))
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertEqual(d["total"], 1)
        self.assertIn("measured", d["quotes"][0]["text"])

        # 新增一条中文，搜索能命中（含 URL 编码的中文）
        st, _, body = http("POST", self.srv.base, "/quotes/add", {"text": "搜索专用独特词汇蓝莓气泡"})
        self.assertEqual(st, 200, body)
        st, _, body = http("GET", self.srv.base, "/quotes?q=" + urllib.parse.quote("蓝莓气泡"))
        d = json.loads(body)
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["quotes"][0]["text"], "搜索专用独特词汇蓝莓气泡")

        # 不存在的词 → 0 条
        st, _, body = http("GET", self.srv.base, "/quotes?q=" + urllib.parse.quote("绝对不存在的词xyz"))
        self.assertEqual(json.loads(body)["total"], 0)

    # ---- 7. 隔离自检：不碰网络/真实目录 ----
    def test_07_isolation_guards(self):
        self.assertFalse(os.path.exists(os.path.join(self.srv.data_dir, "sync.sh")),
                         "临时数据目录不应有 sync.sh（否则 server 会触发 git 同步）")
        http("POST", self.srv.base, "/quotes/add", {"text": "触发一次写入"})
        time.sleep(0.3)
        self.assertTrue(any(f.startswith("quotes-") for f in os.listdir(self.srv.backup_dir)),
                        "备份应写入临时备份目录")
        self.assertNotIn("sync", " ".join(os.listdir(self.srv.data_dir)).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
