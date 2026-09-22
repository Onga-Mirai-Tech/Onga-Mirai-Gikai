"""会議録パーサのテスト。

原文HTMLは private の data リポジトリにあり、public 側のCIからは見えない。
ここでは**構造だけを真似た合成データ**を使う。

内容は実物で見つけた形をそのまま写している。全460件を通して確認した結果、
未分類の発言は0件・未分類の出欠行も0件になった状態を、このテストで固定する。

    python -m unittest discover -s pipeline/tests -t .
"""

from __future__ import annotations

import unittest

from pipeline.parse import transcript as T


def build(header: str, *blocks: str) -> str:
    out = [f'<A NAME="HUID1"></A>{header}']
    for i, b in enumerate(blocks, start=2):
        out.append(f'<P><A NAME="HUID{i}"></A>{b}')
    return "".join(out)


HEADER_REIWA = """令和　８年第　３回定例会<BR><BR>１．議長の氏名　　　織田　隆徳<BR><BR>
２．説明のため出席した者の氏名・職<BR><BR>
　　　町長　　　　　古野　　修<BR>
　　　人事防災課長　藤井紅一郎<BR>
　　　みらいデザイン課長<BR>
　　　　　　　　　　安部　真介<BR><BR>
３．書記の氏名<BR><BR>
　　　議会事務局長　野口　健治<BR>
　　　事務係長　　　仲野　剛司<BR><BR>
４．議員の出欠　（出席 ／・　欠席　△）<BR><BR>
│出欠│ 議席 │　氏　　　名　││出欠│ 議席 │　氏　　　名　│<BR>
│ ／ │１番　│仲　摩　靖　浩││ △ │２番　│野　口　久美子│<BR>
│ ／ │３番　│田　代　順　二││　　│４番　│仲　野　新三郎│<BR>
│ ／ │５番　│立　石　紘一郎││ ／ │６番　│欠　　　　　番│<BR><BR>
日程第１　　議案第４１号　　遠賀町税条例の一部改正について<BR>
日程第２　　一　般　質　問<BR>"""

# 平成期は書記が「氏名 → 職」の順。1行に2人ぶん入ることもある。
HEADER_HEISEI = """平成１８年第　３回定例会<BR><BR>１．議長の氏名　　　仲　野　　丈<BR><BR>
２．説明のため出席した者の氏名・職<BR>
　　　町長　　　　　木　村　隆　治<BR>
　　　収入役　　　　高　　敏　昭<BR><BR>
３．書記の氏名<BR><BR>
　　　吉村　哲司　議会事務局長　　平田　多賀子事務係長<BR><BR>
４．議員の出欠　（出席／・　欠席　△）<BR><BR>
│ ／ │　１番│平　見　光　二││──│　２番│辞　　　　　職│<BR>"""


class TestHeader(unittest.TestCase):
    def setUp(self):
        self.r = T.parse(build(HEADER_REIWA, "○議長（織田隆徳）　開会します。"), "K_R08060400011")
        self.h = T.parse(build(HEADER_HEISEI, "○議長（仲野丈）　開会します。"), "K_H18061600031")

    def test_chair(self):
        self.assertEqual(self.r.chair.name, "織田隆徳")
        self.assertEqual(self.r.chair_title, "議長の氏名")
        self.assertEqual(self.h.chair.name, "仲野丈")

    def test_executives_title_first(self):
        got = {e.title: e.name for e in self.r.executives}
        self.assertEqual(got["町長"], "古野修")
        self.assertEqual(got["人事防災課長"], "藤井紅一郎")

    def test_executive_name_wraps_to_next_line(self):
        # 職が欄に収まらないと、氏名だけが次の行に送られる
        got = {e.title: e.name for e in self.r.executives}
        self.assertEqual(got["みらいデザイン課長"], "安部真介")

    def test_clerks_title_first_in_reiwa(self):
        self.assertEqual(
            [(c.title, c.name) for c in self.r.clerks],
            [("議会事務局長", "野口健治"), ("事務係長", "仲野剛司")],
        )

    def test_clerks_name_first_in_heisei(self):
        # 平成は順序が逆。位置で決め打ちすると職と氏名が入れ替わる。
        self.assertEqual(
            [(c.title, c.name) for c in self.h.clerks],
            [("議会事務局長", "吉村哲司"), ("事務係長", "平田多賀子")],
        )

    def test_attendance_statuses(self):
        got = {a.seat.strip("　"): a.status for a in self.r.attendance}
        self.assertEqual(got["１番"], "出席")
        self.assertEqual(got["２番"], "欠席")
        self.assertEqual(got["４番"], "記載なし")  # 出欠欄が空
        self.assertEqual(got["６番"], "欠番")
        self.assertEqual(self.r.present_count, 3)

    def test_attendance_resigned_seat(self):
        self.assertEqual([a.status for a in self.h.attendance if a.name == "辞職"], ["辞職"])

    def test_agenda(self):
        self.assertEqual(len(self.r.agenda), 2)
        self.assertIn("議案第４１号", self.r.agenda[0])


class TestUtterance(unittest.TestCase):
    def parse_one(self, block: str) -> T.Utterance:
        return T.parse(build(HEADER_REIWA, block), "K_R08060400011").utterances[0]

    def test_marks_map_to_kinds(self):
        self.assertEqual(self.parse_one("○議長（織田隆徳）　はい。").kind, "議長")
        self.assertEqual(self.parse_one("◆３番議員（田代順二）　質問します。").kind, "質問者")
        self.assertEqual(self.parse_one("◎税務課長（井口正彦）　お答えします。").kind, "答弁者")

    def test_speaker_fields(self):
        u = self.parse_one("◆３番議員（田代順二）　３番議員の田代です。")
        self.assertEqual((u.title, u.name), ("３番議員", "田代順二"))
        self.assertEqual(u.text, "３番議員の田代です。")

    def test_label_without_title(self):
        # 「◎（堅田繁）」のように職の記載がないことがある
        u = self.parse_one("◎（堅田繁）　結局、今の介護保険ですと。")
        self.assertEqual((u.title, u.name, u.kind), ("", "堅田繁", "答弁者"))

    def test_very_long_title(self):
        long = "遠賀町議会の運営に関する基準の一部改正のための特別委員会委員長"
        u = self.parse_one(f"◎{long}（濱田竜一）　委員長報告を行います。")
        self.assertEqual((u.title, u.name), (long, "濱田竜一"))

    def test_label_with_honorific_and_no_parens(self):
        # 参考人・受章者・任命された委員の挨拶。括弧を使わず敬称で締める。
        for label, name in [("山中巧吉氏", "山中巧吉"), ("西村朗さん", "西村朗")]:
            u = self.parse_one(f"◎{label}　おはようございます。")
            self.assertEqual((u.title, u.name, u.kind), ("", name, "答弁者"))
            self.assertEqual(u.name_raw, label)

    def test_agenda_heading(self):
        u = self.parse_one("△日程第１０<BR>　これより、議案質疑に入ります。")
        self.assertEqual((u.kind, u.agenda_no), ("日程", 10))
        self.assertEqual(u.text, "これより、議案質疑に入ります。")

    def test_additional_agenda_heading(self):
        u = self.parse_one("△追加日程第４<BR>　「選挙第２号」を議題と致します。")
        self.assertEqual((u.kind, u.agenda_no), ("追加日程", 4))

    def test_notes_are_extracted(self):
        u = self.parse_one(
            "○議長（織田隆徳）　賛成の諸君の起立を求めます。<BR>"
            "　───　賛成者起立　───　　　　１１：１〔１３〕<BR>"
        )
        self.assertIn("賛成者起立", u.notes[0])

    def test_sequence_and_huid(self):
        tr = T.parse(
            build(HEADER_REIWA, "○議長（織田隆徳）　一。", "◆２番議員（野口久美子）　二。"),
            "K_R08060400011",
        )
        self.assertEqual([(u.seq, u.huid) for u in tr.utterances], [(1, 2), (2, 3)])


class TestSqueeze(unittest.TestCase):
    def test_squeeze(self):
        self.assertEqual(T.squeeze("令和　８年第　３回定例会"), "令和8年第3回定例会")
        self.assertEqual(T.squeeze("仲　野　新三郎"), "仲野新三郎")


if __name__ == "__main__":
    unittest.main()
