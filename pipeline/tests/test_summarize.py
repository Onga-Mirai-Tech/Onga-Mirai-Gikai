"""要約の入力づくりと、機械的な突き合わせのテスト。

要約そのものの良し悪しは人が読んで決めるしかないが、
**原文にない数字を書いていないか**と**引用した発言が実在するか**は機械で判定できる。
この2つは公開してよいかどうかの線引きなので、テストで固定する。

    python -m unittest discover -s pipeline/tests -t .
"""

from __future__ import annotations

import unittest

from pipeline.summarize import compare as X
from pipeline.summarize import prompt as P
from pipeline.summarize import run as R


SOURCE = """【議案】令和3年第2回定例会
議案第25号　令和３年度遠賀町一般会計予算

会議録:

[41305] 答弁者 町長 古野修
地方特例交付金8,450万円、前年対比6,350万円の増でございます。

[41717] 質問者 ３番議員 田代順二
反対討論を行います。
"""


class TestUnmatchedNumbers(unittest.TestCase):
    def test_finds_a_number_that_is_not_in_the_source(self):
        # 実際に起きた誤り: 「6,350万円」を「6億3,500万円」と書いた（10倍）
        self.assertEqual(X.unmatched_numbers("6億3,500万円の増", SOURCE), ["3,500"])

    def test_accepts_the_same_number_written_differently(self):
        # 全角・半角・桁区切りの違いは誤りではない
        self.assertEqual(X.unmatched_numbers("８４５０万円", SOURCE), [])
        self.assertEqual(X.unmatched_numbers("8450万円", SOURCE), [])

    def test_accepts_numbers_that_are_in_the_source(self):
        self.assertEqual(X.unmatched_numbers("6,350万円の増です。", SOURCE), [])

    def test_accepts_the_man_notation_the_minutes_use(self):
        # 会議録は「５万6,100円」と書き、要約は「56,100円」と書く。値は同じ。
        src = "小学校の給食費は年間５万6,100円です。"
        self.assertEqual(X.unmatched_numbers("年間56,100円です。", src), [])

    def test_still_catches_a_wrong_order_of_magnitude(self):
        # 万表記を許しても、桁の取り違えは拾えなければならない
        src = "前年対比6,350万円の増でございます。"
        self.assertEqual(X.unmatched_numbers("6億3,500万円の増", src), ["3,500"])

    def test_accepts_the_oku_notation(self):
        # 86億4,762万円 = 8,647,620,000円
        self.assertEqual(X.unmatched_numbers("8,647,620,000円", "総額86億4,762万円です。"), [])

    def test_flags_a_fabricated_year(self):
        # 実際に起きた誤り: 原文にない西暦を計算して書いた
        self.assertEqual(X.unmatched_numbers("2021年時点で", SOURCE), ["2021"])


class TestBadHuids(unittest.TestCase):
    def test_detects_a_huid_that_does_not_exist(self):
        data = {"points": [{"kind": "討論", "text": "x", "huids": [41717, 99999]}]}
        self.assertEqual(X.bad_huids(data, SOURCE), [99999])

    def test_accepts_huids_in_the_source(self):
        data = {"points": [{"kind": "提案理由", "text": "x", "huids": [41305]}]}
        self.assertEqual(X.bad_huids(data, SOURCE), [])

    def test_reads_the_nested_shape_of_a_general_question(self):
        # 一般質問は topics[].points[].huids の2段
        data = {"topics": [{"no": 1, "title": "t",
                            "points": [{"question": "q", "answer": "a", "huids": [41305, 12345]}]}]}
        self.assertEqual(X.bad_huids(data, SOURCE), [12345])


class TestVerify(unittest.TestCase):
    def test_a_summary_without_sources_is_not_verified(self):
        # 根拠がない要約は原文と照合できない。公開してよいかの線引きに使う。
        r = R.verify("bill", {"points": [{"kind": "討論", "text": "x", "huids": []}]}, SOURCE)
        self.assertFalse(r["verified"])
        self.assertEqual([i["kind"] for i in r["issues"]], ["根拠の発言IDがない"])

    def test_a_clean_summary_is_verified(self):
        r = R.verify("bill", {"points": [{"kind": "提案理由", "text": "8,450万円です。",
                                          "huids": [41305]}]}, SOURCE)
        self.assertTrue(r["verified"], r["issues"])

    def test_a_wrong_number_is_flagged(self):
        r = R.verify("bill", {"points": [{"kind": "提案理由", "text": "6億3,500万円の増。",
                                          "huids": [41305]}]}, SOURCE)
        self.assertFalse(r["verified"])
        self.assertIn("原文にない数字", [i["kind"] for i in r["issues"]])


class TestCustomId(unittest.TestCase):
    def test_is_safe_for_the_batch_api(self):
        # custom_id に使えるのは英数字と `_-` だけ。議案IDは日本語を含むので使えない。
        cid = R.custom_id("bill", 42)
        self.assertTrue(cid.replace("_", "").replace("-", "").isalnum(), cid)
        self.assertLessEqual(len(cid), 64)

    def test_threads_and_bills_do_not_collide(self):
        self.assertNotEqual(R.custom_id("thread", 1), R.custom_id("bill", 1))


class TestPrompt(unittest.TestCase):
    def test_the_tool_is_forced(self):
        # 地の文でJSONを書かせると前置きが混じる。必ずツールで受け取る。
        r = P.build("thread", "x", "claude-sonnet-5")
        self.assertEqual(r["tool_choice"], {"type": "tool", "name": "record_summary"})

    def test_a_general_question_keeps_the_notice_items_one_to_one(self):
        # 通告書の項目と1対1でないと、通告書との突き合わせ（検証）ができない
        items = P.THREAD_TOOL["input_schema"]["properties"]["topics"]["items"]
        self.assertEqual(items["required"], ["no", "title", "points"])
        self.assertIn("1対1", P.THREAD_TOOL["input_schema"]["properties"]["topics"]["description"])

    def test_every_point_must_carry_its_sources(self):
        for tool in (P.THREAD_TOOL, P.BILL_TOOL):
            sch = tool["input_schema"]["properties"]
            leaf = (sch["topics"]["items"]["properties"]["points"] if "topics" in sch
                    else sch["points"])
            self.assertIn("huids", leaf["items"]["required"], tool["name"])

    def test_version_is_recorded(self):
        self.assertRegex(P.PROMPT_VERSION, r"^\d{4}-\d{2}-\d{2}\.\d+$")


if __name__ == "__main__":
    unittest.main()
