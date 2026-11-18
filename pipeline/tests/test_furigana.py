"""ふりがな（pipeline/build/furigana.py）。"""

import re
import unittest

from pipeline.build import furigana as F
from pipeline.build import site as S


def plain(html_text: str) -> str:
    """読みを取り除いた文字。元の文と同じでなければならない。"""
    return re.sub(r"<rt>[^<]*</rt>|</?ruby>", "", html_text)


class TestAlign(unittest.TestCase):
    def test_kanji_only(self):
        self.assertEqual(F.align("議会", "ぎかい"), [("議会", "ぎかい")])

    def test_okurigana_is_not_covered(self):
        self.assertEqual(F.align("話さ", "はなさ"), [("話", "はな"), ("さ", None)])
        self.assertEqual(F.align("取り組み", "とりくみ"),
                         [("取", "と"), ("り", None), ("組", "く"), ("み", None)])

    def test_mismatch_gives_up(self):
        self.assertIsNone(F.align("話さ", "かたる"))


class TestText(unittest.TestCase):
    def setUp(self):
        self.fu = F.Furigana({"遠賀町": "おんがちょう", "令和": "れいわ"}, {"古野"})

    def test_reads_common_words(self):
        self.assertIn("<ruby>議会<rt>ぎかい</rt></ruby>", self.fu.text("議会で話す"))

    def test_text_is_unchanged(self):
        for s in ["変更も行っています。", "古野町長は、令和7年10日に答えた。",
                  "&amp;と&quot;議会&quot;", "１０日　改行\n次", "ふるさと納税の取り組み"]:
            self.assertEqual(plain(self.fu.text(s)), s)

    def test_proper_nouns_only_from_checked_list(self):
        # 遠賀町は確かめた読み、別府は辞書の読み（べっぷ）が町での呼び方と違いうるので付けない
        got = self.fu.text("遠賀町の別府地区")
        self.assertIn("<ruby>遠賀町<rt>おんがちょう</rt></ruby>", got)
        self.assertNotIn("べっぷ", got)

    def test_names_are_not_read(self):
        # 「古野町（地名）＋長」と切られないよう、氏名を先に除く
        got = self.fu.text("古野町長は答えた")
        self.assertTrue(got.startswith("古野<ruby>町長<rt>ちょうちょう</rt></ruby>"), got)

    def test_place_name_that_is_also_a_surname(self):
        fu = F.Furigana({"田代": "たしろ", "別府": "べふ", "上別府": "かみべふ"}, {"田代", "田代順二"})
        self.assertTrue(fu.text("田代地区").startswith("<ruby>田代<rt>たしろ</rt></ruby>"))
        self.assertNotIn("たしろ", fu.text("田代議員は反対した"))
        self.assertNotIn("たしろ", fu.text("田代順二議員"))
        self.assertIn("<ruby>上別府<rt>かみべふ</rt></ruby>", fu.text("町道老良上別府線"))
        self.assertTrue(fu.text("別府広場").startswith("<ruby>別府<rt>べふ</rt></ruby>"))

    def test_school_abbreviation(self):
        self.assertNotIn("<ruby>小", F.Furigana({"広渡": "ひろわたり"}).text("広渡小で"))

    def test_counters_whose_reading_depends_on_the_number(self):
        got = self.fu.text("10日に3人が")
        self.assertNotIn("<ruby>日", got)
        self.assertNotIn("<ruby>人", got)
        self.assertIn("<ruby>件<rt>けん</rt></ruby>", self.fu.text("3件"))

    def test_ambiguous_words_are_skipped(self):
        self.assertNotIn("<ruby>行", self.fu.text("変更も行っています"))
        self.assertNotIn("<ruby>方", self.fu.text("お持ちの方を対象に"))


class TestAnnotate(unittest.TestCase):
    def setUp(self):
        self.fu = F.Furigana()

    def test_source_text_is_not_read(self):
        got = self.fu.annotate("<p>議会" + S.esc("会議録の発言") + "</p>")
        self.assertIn("<ruby>議会", got)
        self.assertIn("会議録の発言", got)
        self.assertNotIn(F.SRC_OPEN, got)

    def test_attributes_and_options_are_left_alone(self):
        got = self.fu.annotate('<a title="議会">原文</a><select><option>議案</option></select>')
        self.assertIn('title="議会"', got)
        self.assertIn("<option>議案</option>", got)
        self.assertIn("<ruby>原文", got)

    def test_head_is_left_alone(self):
        got = self.fu.annotate("<head><title>議会</title></head><body>議会</body>")
        self.assertIn("<title>議会</title>", got)

    def test_marks_inside_attributes_are_removed(self):
        got = self.fu.annotate(f'<a href="{S.esc("noguchi")}">x</a>')
        self.assertEqual(got, '<a href="noguchi">x</a>')


if __name__ == "__main__":
    unittest.main()
