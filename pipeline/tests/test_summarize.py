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
