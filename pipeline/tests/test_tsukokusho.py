"""一般質問通告書のテスト。

PDFそのものは private の data リポジトリにあるので、ここではテキストになった
あとの処理を試す。実データで間違えて直した箇所を固定する。

- 11件は pypdf がフォントの符号化を解けず、描画命令から読む。そちらは
  括弧や数字が1文字ずつ別の行になるため、そのままでは要旨の番号を
  質問事項の番号と取り違える
- 文中の数字（「平成33／年度のごみ削減目標…」）が行頭に来ることがある。
  質問事項の番号は1から連番になる性質で振り落とす
- 会議名では年月を決められない。平成期は「遠賀町議会第４回９月定例会」で
  元号年が入っていない

    python -m unittest discover -s pipeline/tests -t .
"""

from __future__ import annotations

import unittest

from pipeline.parse import threads as TH
from pipeline.parse import tsukokusho as TS


class TestTopics(unittest.TestCase):
    def test_title_and_points_on_one_line(self):
        # 令和期。番号・見出し・最初の要旨が同じ行に入る。
        block = "１  高齢者支援について ⑴ 現在、遠賀町の第１号被保険者の要介護…\n"
        self.assertEqual([t.title for t in TS.parse_topics(block)], ["高齢者支援について"])

    def test_title_wraps_across_lines(self):
        block = "１ 様々な困難を抱える\n人への支援について \n(1) ヤングケアラーへの支援について \n"
        self.assertEqual([t.title for t in TS.parse_topics(block)], ["様々な困難を抱える人への支援について"])

    def test_multiple_items(self):
        block = (
            "１ コミュニティバスについて\n"
            "⑴ バス停を設置すべきではないか。\n"
            "２ ＪＲ鹿児島本線減便について\n"
            "⑴ 増便を申し入れるべきではないか。\n"
        )
        self.assertEqual(
            [(t.no, t.title) for t in TS.parse_topics(block)],
            [(1, "コミュニティバスについて"), (2, "ＪＲ鹿児島本線減便について")],
        )

    def test_column_headers_are_stripped(self):
        block = "１ ごみの減量化について 質問の相手 町 長\n⑴ 目標を問う。\n"
        self.assertEqual([t.title for t in TS.parse_topics(block)], ["ごみの減量化について"])


class TestSequenceFilter(unittest.TestCase):
    def make(self, *pairs):
        return [TS.Topic(no=n, title=t) for n, t in pairs]

    def test_keeps_consecutive_from_one(self):
        kept, dropped = TS._keep_sequential(self.make((1, "ごみ"), (2, "コミュニティー")))
        self.assertEqual([t.no for t in kept], [1, 2])
        self.assertEqual(dropped, [])

    def test_drops_sentence_numbers(self):
        # 「平成33／年度のごみ削減目標…」が行頭に来て番号に見えるケース
        kept, dropped = TS._keep_sequential(
            self.make((1, "ごみの減量化について"), (33, "年度のごみ削減目標を問う。"), (2, "地域コミュニティー組織について"))
        )
        self.assertEqual([t.title for t in kept], ["ごみの減量化について", "地域コミュニティー組織について"])
        self.assertEqual(len(dropped), 1)
        self.assertIn("33.", dropped[0])


class TestRejoin(unittest.TestCase):
    def test_single_characters_are_rejoined(self):
        # 描画命令から読むと（ 1 ）が3行に割れる。そのままだと1が番号に見える。
        text = "（\n1\n）\n７月５日の豪雨時対応について\n"
        self.assertNotIn("\n1\n", TS._rejoin_short_lines(text))


class TestYearMonth(unittest.TestCase):
    def test_from_filename(self):
        self.assertEqual(TH.notice_year_month("12067_令和8年6月.pdf"), "2026-06")
        self.assertEqual(TH.notice_year_month("2890_平成22年9月.pdf"), "2010-09")

    def test_unreadable_filename(self):
        self.assertIsNone(TH.notice_year_month("9346_一般質問通告書（令和8年）-9346.pdf"))


class TestAttach(unittest.TestCase):
    def thread(self, name, on="2026-06-09"):
        return TH.Thread(id="x", meeting_id="2026-t3", meeting="令和8年第3回定例会",
                         unid="K_R08060900031", on=on, order=1,
                         questioner=name, questioner_title="２番議員")

    def notice(self, name, *titles):
        return TS.Notice(order=1, questioner=name,
                         topics=[TS.Topic(no=i + 1, title=t) for i, t in enumerate(titles)])

    def test_attaches_by_month_and_name(self):
        t = self.thread("野口久美子")
        TH.attach_topics([t], {"2026-06": [self.notice("野口久美子", "敷地内禁煙について")]})
        self.assertEqual([x["title"] for x in t.topics], ["敷地内禁煙について"])
        self.assertEqual(t.note, "")

    def test_unmatched_is_flagged(self):
        t = self.thread("野口久美子")
        TH.attach_topics([t], {})
        self.assertIn("通告書と対応づかず", t.note)

    def test_alias_applies_to_both_sides(self):
        # 会議録が「濱岡峯達」、通告書が「浜岡峯達」。片側だけに当てると逆に外れる。
        t = self.thread("濱岡峯達")
        notices = {"2026-06": [self.notice("浜岡峯達", "コミュニティバスについて")]}
        TH.attach_topics([t], notices, {"濱岡峯達": "浜岡峯達"})
        self.assertEqual(t.questioner, "浜岡峯達")
        self.assertEqual([x["title"] for x in t.topics], ["コミュニティバスについて"])


if __name__ == "__main__":
    unittest.main()
