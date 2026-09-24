"""議案抽出のテスト。

実物で確かめた事実を固定する。とくに次の2つは実データで間違えた箇所。

- 「討論は、ございませんか」は**問いかけ**であって不在の宣言ではない。
  実体の有無はト書き（───　討論なし　───）だけで判定する
- 「それぞれ所管ごとに第一・第二常任委員会に付託」は**分割付託**。両方を拾う

令和8年第3回定例会の12件は、町HPの「審議案件・結果」PDFと突き合わせて
議決結果・議決日とも全件一致することを確認済み。

    python -m unittest discover -s pipeline/tests -t .
"""

from __future__ import annotations

import unittest
from datetime import date

from pipeline.parse import bills as B
from pipeline.parse import transcript as T
from pipeline.tests.test_transcript import build

HEADER = """令和　８年第　３回定例会<BR><BR>１．議長の氏名　　　織田　隆徳<BR><BR>
２．説明のため出席した者の氏名・職<BR><BR>
　　　町長　　　　　古野　　修<BR><BR>
３．書記の氏名<BR><BR>
　　　議会事務局長　野口　健治<BR><BR>
４．議員の出欠　（出席 ／・　欠席　△）<BR><BR>
│ ／ │３番　│田　代　順　二│<BR><BR>
日程第１　　議案第４１号　　遠賀町税条例の一部改正について<BR>
日程第２　　議案第４２号　　遠賀町国民健康保険税条例の一部改正について<BR>
日程第９　　議案第４９号　　令和８年度遠賀町一般会計補正予算(第１号)<BR>"""

ON = date(2026, 6, 8)


def day(*blocks: str) -> dict:
    tr = T.parse(build(HEADER, *blocks), "K_R08060800021")
    return B.extract_day(tr, ON)


class TestMeetingId(unittest.TestCase):
    def test_teireikai(self):
        self.assertEqual(B.meeting_id("令和　８年第　３回定例会"), ("2026-t3", "令和8年第3回定例会"))

    def test_rinjikai(self):
        self.assertEqual(B.meeting_id("令和　８年第　１回臨時会"), ("2026-r1", "令和8年第1回臨時会"))

    def test_heisei(self):
        self.assertEqual(B.meeting_id("平成１８年第　６回定例会")[0], "2006-t6")


class TestAgendaMapping(unittest.TestCase):
    def test_agenda_line_gives_number_and_title(self):
        ev = day("△日程第１<BR>　これより、議案質疑に入ります。")
        self.assertIn("議案第41号", ev)
        self.assertEqual(ev["議案第41号"]["title"], "遠賀町税条例の一部改正について")

    def test_non_bill_agenda_is_ignored(self):
        ev = day("△日程第５<BR>　会期の決定を議題と致します。")
        self.assertEqual(ev, {})


class TestStages(unittest.TestCase):
    def test_question_absent_from_note(self):
        ev = day(
            "△日程第１<BR>　これより、議案質疑に入ります。質疑はございませんか。<BR>"
            "　───　質疑なし　───<BR>"
        )
        self.assertIs(ev["議案第41号"]["content"]["質疑"], False)

    def test_question_present(self):
        ev = day(
            "△日程第１<BR>　これより、議案質疑に入ります。質疑を許します。",
            "◆３番議員（田代順二）　税収はいくら減るのか尋ねます。",
        )
        self.assertIs(ev["議案第41号"]["content"]["質疑"], True)

    def test_asking_whether_there_is_debate_is_not_absence(self):
        # 「討論は、ございませんか」は問いかけ。直後に討論があれば「討論あり」。
        ev = day(
            "△日程第２<BR>　これより、討論に入ります。討論は、ございませんか。田代議員。",
            "◆３番議員（田代順二）　反対の立場で討論します。",
        )
        self.assertIs(ev["議案第42号"]["content"]["討論"], True)

    def test_debate_absent_from_note(self):
        ev = day(
            "△日程第２<BR>　これより、討論に入ります。討論は、ございませんか。<BR>"
            "　───　討論なし　───<BR>"
        )
        self.assertIs(ev["議案第42号"]["content"]["討論"], False)


class TestReferral(unittest.TestCase):
    def test_single_committee(self):
        ev = day("△日程第１<BR>　議案第４１号については、第一常任委員会に付託致します。")
        got = ev["議案第41号"]
        self.assertEqual((got["referral"], got["committees"]), ("committee", ["第一常任委員会"]))

    def test_split_referral(self):
        ev = day("△日程第９<BR>　それぞれ所管ごとに第一・第二常任委員会に付託致します。")
        self.assertEqual(ev["議案第49号"]["committees"], ["第一常任委員会", "第二常任委員会"])

    def test_omitted(self):
        ev = day(
            "△日程第１<BR>　会議規則第39条第３項の規定により、委員会付託を省略致したいと思います。"
        )
        self.assertEqual(ev["議案第41号"]["referral"], "omitted")

    def test_split_committees_helper(self):
        self.assertEqual(B.split_committees("第一・第二常任委員会"), ["第一常任委員会", "第二常任委員会"])
        self.assertEqual(B.split_committees("議会運営委員会"), ["議会運営委員会"])


class TestVoteAndResult(unittest.TestCase):
    def test_standing_vote_with_tally(self):
        ev = day(
            "△日程第２<BR>　賛成の諸君の起立を求めます。<BR>"
            "　───　賛成者起立　───　　　　　１１：１〔１３〕<BR>"
        )
        v = ev["議案第42号"]["vote"]
        self.assertEqual((v.method, v.yes, v.no, v.present), ("起立", 11, 1, 13))

    def test_no_objection_vote(self):
        ev = day("△日程第１<BR>　ご異議ございませんか。<BR>　───　異議なしの声　───<BR>")
        self.assertEqual(ev["議案第41号"]["vote"].method, "異議なし")

    def test_result_passive_form(self):
        # 「可決されました」。受身形を落とすと結果が9.5%しか取れなくなる。
        ev = day("△日程第１<BR>　よって、本案は委員長報告のとおり可決されました。")
        self.assertEqual(ev["議案第41号"]["result"], "可決")

    def test_result_decided_form(self):
        ev = day("△日程第２<BR>　よって、議案第４２号は、承認することに決しました。")
        self.assertEqual(ev["議案第42号"]["result"], "承認")

    def test_report_has_no_result(self):
        ev = day("△日程第１<BR>　これより、議案質疑に入ります。")
        self.assertIsNone(ev["議案第41号"]["result"])


class TestBillNumber(unittest.TestCase):
    def test_normalises_fullwidth(self):
        self.assertEqual(B.bill_number("議案", "４１"), "議案第41号")


if __name__ == "__main__":
    unittest.main()


class TestBulkAgenda(unittest.TestCase):
    """一括議題（「議案第50号から議案第59号までを、一括して議題と致します」）。

    決算認定は必ずこの形で処理される。範囲を開かないと、委員長報告・討論・採決を
    まるごと取りこぼす（実測: 令和分の議案55件で議決結果が取れていなかった）。
    """

    AMAP = {6: ("議案第50号", "一般会計決算"), 7: ("議案第51号", "国保決算"),
            8: ("議案第52号", "霊園決算")}

    def test_expands_a_range(self):
        got = B._range_numbers("議案第５０号から、議案第５２号までを、一括して議題と致します。", self.AMAP)
        self.assertEqual(got, ["議案第50号", "議案第51号", "議案第52号"])

    def test_only_numbers_on_the_agenda(self):
        # 範囲の間に欠番があることがある。機械的に埋めると存在しない議案を作る。
        got = B._range_numbers("議案第５０号から、議案第５９号までを、一括して議題と致します。", self.AMAP)
        self.assertEqual(got, ["議案第50号", "議案第51号", "議案第52号"])

    def test_a_single_bill_is_not_a_range(self):
        self.assertEqual(B._range_numbers("議案第５０号を議題と致します。", self.AMAP), [])

    def test_mentions_finds_the_named_bill(self):
        # 一括議題でも議決は議案ごとに宣言される。どの議案の話かを番号で見分ける。
        sent = "よって、議案第５０号は、認定することに決しました。"
        self.assertTrue(B._mentions(sent, "議案第50号"))
        self.assertFalse(B._mentions(sent, "議案第51号"))
