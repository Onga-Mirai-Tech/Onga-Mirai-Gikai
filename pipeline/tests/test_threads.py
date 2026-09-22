"""一般質問スレッドのテスト。

実データで間違えて直した箇所を固定する。

- 締めの文言（「以上で、◯◯議員の一般質問は終了致しました」）は後年しか
  使われていない。154日中67日で不在なので、これを区切りに使うと平成期が落ちる
- 追加日程は一般質問ではない。日程番号だけで照合すると、緊急動議の質疑応答を
  一般質問として拾う
- 長い一般質問の途中に別の議員が割り込むことがある（議事進行など）。
  他の議員の区間に収まる質問者は独立したスレッドにしない

    python -m unittest discover -s pipeline/tests -t .
"""

from __future__ import annotations

import unittest
from datetime import date

from pipeline.parse import threads as TH
from pipeline.parse import transcript as T
from pipeline.tests.test_transcript import build

HEADER = """令和　８年第　３回定例会<BR><BR>１．議長の氏名　　　織田　隆徳<BR><BR>
２．説明のため出席した者の氏名・職<BR><BR>
　　　町長　　　　　古野　　修<BR><BR>
３．書記の氏名<BR><BR>
　　　議会事務局長　野口　健治<BR><BR>
４．議員の出欠　（出席 ／・　欠席　△）<BR><BR>
│ ／ │２番　│野　口　久美子││ ／ │３番　│田　代　順　二│<BR><BR>
日程第１　　議案第４１号　　遠賀町税条例の一部改正について<BR>
日程第２　　一　般　質　問<BR>"""

ON = date(2026, 6, 9)


def day(*blocks: str) -> list[TH.Thread]:
    tr = T.parse(build(HEADER, *blocks), "K_R08060900031")
    return TH.split_threads(tr, ON)


class TestSection(unittest.TestCase):
    def test_agenda_numbers(self):
        tr = T.parse(build(HEADER, "○議長（織田隆徳）　開会。"), "K_R08060900031")
        self.assertEqual(TH.question_agenda_nos(tr), {2})

    def test_no_general_question_day(self):
        tr = T.parse(
            build("令和　８年第　３回定例会<BR><BR>日程第１　　議案第４１号　　条例改正<BR>",
                  "○議長（織田隆徳）　開会。"),
            "K_R08060400011",
        )
        self.assertIsNone(TH.find_section(tr))

    def test_opener_starts_the_section(self):
        tr = T.parse(
            build(HEADER,
                  "○議長（織田隆徳）　開会します。",
                  "△日程第１<BR>　議案第４１号の質疑に入ります。",
                  "○議長（織田隆徳）　これより、通告順に従い、一般質問を許します。２番議員、野口久美子議員。",
                  "◆２番議員（野口久美子）　質問します。"),
            "K_R08060900031",
        )
        start, _end = TH.find_section(tr)
        self.assertEqual(start, 3)


class TestSplit(unittest.TestCase):
    def test_threads_in_order(self):
        got = day(
            "○議長（織田隆徳）　これより、通告順に従い、一般質問を許します。２番議員、野口久美子議員。",
            "◆２番議員（野口久美子）　敷地内禁煙について尋ねます。",
            "◎町長（古野修）　お答えします。",
            "○議長（織田隆徳）　田代議員。",
            "◆３番議員（田代順二）　公営住宅について尋ねます。",
            "◎町長（古野修）　お答えします。",
        )
        self.assertEqual([(t.order, t.questioner) for t in got],
                         [(1, "野口久美子"), (2, "田代順二")])
        self.assertEqual(got[0].questioner_title, "２番議員")
        self.assertEqual(got[0].responders, ["古野修"])

    def test_nested_interruption_is_not_a_thread(self):
        # 田代議員の発言が野口議員の質問の途中に挟まる（議事進行など）。
        # 野口議員の区間に収まるので、独立したスレッドにしない。
        got = day(
            "○議長（織田隆徳）　これより、一般質問を許します。野口久美子議員。",
            "◆２番議員（野口久美子）　一点目を尋ねます。",
            "◆３番議員（田代順二）　議長、議事進行。",
            "○議長（織田隆徳）　野口議員。",
            "◆２番議員（野口久美子）　二点目を尋ねます。",
        )
        self.assertEqual([t.questioner for t in got], ["野口久美子"])
        self.assertIn("田代順二", got[0].note)

    def test_section_ends_at_next_agenda(self):
        got = day(
            "○議長（織田隆徳）　これより、一般質問を許します。野口久美子議員。",
            "◆２番議員（野口久美子）　質問します。",
            "△日程第３<BR>　議案第４２号の採決に入ります。",
            "◆３番議員（田代順二）　討論します。",
        )
        self.assertEqual([t.questioner for t in got], ["野口久美子"])

    def test_additional_agenda_is_not_general_question(self):
        # 追加日程第2 は日程第2（一般質問）と番号が同じでも別物
        tr = T.parse(
            build(HEADER,
                  "△追加日程第２<BR>　「特別委員会の設置」を議題と致します。",
                  "◆３番議員（田代順二）　趣旨説明を求めます。"),
            "K_R08060900031",
        )
        self.assertIsNone(TH.find_section(tr))

    def test_turns_record_position(self):
        got = day(
            "○議長（織田隆徳）　これより、一般質問を許します。野口久美子議員。",
            "◆２番議員（野口久美子）　質問します。",
        )
        self.assertEqual([t.kind for t in got[0].turns], ["質問者"])
        self.assertEqual(got[0].turns[0].seq, 2)


if __name__ == "__main__":
    unittest.main()
