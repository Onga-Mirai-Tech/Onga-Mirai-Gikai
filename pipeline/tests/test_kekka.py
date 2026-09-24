"""「審議案件・結果」PDFの読み取りと、議決結果の照合のテスト。

このPDFは会議録とは**独立した第二の情報源**で、会議録の読み取りを検算するために使う。
検算する側が間違っていると、正しい会議録を誤りと判定してしまう。

    python -m unittest discover -s pipeline/tests -t .
"""

from __future__ import annotations

import unittest

from pipeline.parse import kekka as K
from pipeline.verify import results as V


class TestResultWords(unittest.TestCase):
    def test_longer_words_win(self):
        # 「採択」を先に試すと「不採択」が「採択」になり、賛否が逆になる
        self.assertEqual(K._find_result("学校給食の無償化を求める請願不採択"),
                         ("学校給食の無償化を求める請願", "不採択"))
        self.assertEqual(K._find_result("意見書の採択を求める陳情書採択"),
                         ("意見書の採択を求める陳情書", "採択"))

    def test_gengan_kaketsu(self):
        self.assertEqual(K._find_result("条例の一部改正について原案可決"),
                         ("条例の一部改正について", "原案可決"))

    def test_no_result(self):
        self.assertIsNone(K._find_result("条例の一部改正について"))


class TestMeetingId(unittest.TestCase):
    def test_reads_a_spaced_title(self):
        # 表題は「令和７年 第５回遠賀町議会 ６月定例会」のように空白で字送りされる
        self.assertEqual(K.meeting_id("令和７年 第５回遠賀町議会 ６月定例会 審議案件・結果"), "2025-t5")

    def test_first_year_of_an_era(self):
        # 「元」は数字ではない
        self.assertEqual(K.meeting_id("令和元年 第３回遠賀町議会 ７月臨時会"), "2019-r3")

    def test_unknown_title(self):
        self.assertEqual(K.meeting_id("審議案件・結果"), "")


class TestEraBase(unittest.TestCase):
    def test_reads_the_era_from_the_meeting_name(self):
        self.assertEqual(K.era_base("令和元年 第５回遠賀町議会 ９月定例会"), 2018)

    def test_is_not_fooled_by_a_bill_title(self):
        # 議案名の「平成30年度…決算」を会議の元号と読むと、議決年月日が30年ずれる
        text = "令和元年 第５回遠賀町議会 ９月定例会 議案第50号 平成30年度遠賀町一般会計歳入歳出決算"
        self.assertEqual(K.era_base(text), 2018)

    def test_a_continuation_page_has_no_era(self):
        # 表の続きのページには表題がない。呼び出し側が前のページから引き継ぐ。
        self.assertIsNone(K.era_base("議案第55号 平成30年度遠賀町地域下水道事業特別会計"))

    def test_the_caller_can_supply_the_era(self):
        got = K.parse("議案第55号平成30年度下水道決算の認定について1.9.20認定", base=2018)
        self.assertEqual([(i.number, i.decided_on, i.result) for i in got],
                         [("議案第55号", "2019-09-20", "認定")])


class TestDate(unittest.TestCase):
    def test_splits_the_date_from_the_title(self):
        # 議決年月日の欄は元号を書かない（「7.3.21」）。元号は表題から取る。
        self.assertEqual(K.split_date("遠賀町町道路線の廃止について7.3.21", 2018),
                         ("遠賀町町道路線の廃止について", "2025-03-21"))

    def test_without_an_era_the_date_is_not_guessed(self):
        self.assertEqual(K.split_date("件名7.3.21", None), ("件名7.3.21", ""))


class TestNormalizeResult(unittest.TestCase):
    def test_same_meaning(self):
        self.assertEqual(V.normalize_result("原案可決"), "可決")

    def test_amended_is_not_the_same(self):
        # 修正されたかどうかは事実として違う。まとめない。
        self.assertEqual(V.normalize_result("修正可決"), "修正可決")


if __name__ == "__main__":
    unittest.main()
