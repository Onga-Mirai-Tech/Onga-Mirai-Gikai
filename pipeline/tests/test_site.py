"""静的サイト生成のテスト。

出力するHTMLそのものより、**URLになる名前**と**原文の出し方**を固定する。
URLは一度公開したら変えられないし、原文は会議録の記載をそのまま出す必要がある。

    python -m unittest discover -s pipeline/tests -t .
"""

from __future__ import annotations

import unittest

from pipeline.build import site as S


class TestSlugs(unittest.TestCase):
    def test_bill_slug_is_url_safe(self):
        # 議案IDは日本語を含む。URLに使える名前に直す。
        self.assertEqual(S.bill_slug("2026-t3-b議案第42号"), "2026-t3-b42")
        self.assertEqual(S.bill_slug("2026-t3-b発議第1号"), "2026-t3-h1")
        self.assertEqual(S.bill_slug("2026-t3-b報告第7号"), "2026-t3-r7")

    def test_bill_slug_has_no_japanese(self):
        for bid in ("2026-t3-b意見書案第3号", "2026-t3-b請願第1号", "2026-t3-b陳情第2号"):
            self.assertTrue(S.bill_slug(bid).isascii(), bid)

    def test_thread_slug_includes_the_day(self):
        # 一般質問が2日に分かれる定例会があるため、日付を入れないと衝突する。
        a = S.thread_slug({"meeting_id": "2026-t3", "on": "2026-06-09", "order": 1})
        b = S.thread_slug({"meeting_id": "2026-t3", "on": "2026-06-10", "order": 1})
        self.assertNotEqual(a, b)
        self.assertEqual(a, "2026-t3-20260609-q01")


class TestParagraphs(unittest.TestCase):
    def test_splits_on_newlines(self):
        got = S.paragraphs("一点目です。\n　二点目です。")
        self.assertEqual(got, "<p>一点目です。</p><p>二点目です。</p>")

    def test_escapes_html(self):
        self.assertIn("&lt;script&gt;", S.paragraphs("<script>"))

    def test_keeps_transcript_notes(self):
        # ト書きは会議録の記載なので落とさない
        self.assertIn("賛成者起立", S.paragraphs("　───　賛成者起立　───　　１１：１〔１３〕"))

    def test_empty_text(self):
        self.assertEqual(S.paragraphs(""), "<p></p>")


class TestEscape(unittest.TestCase):
    def test_quotes_are_escaped(self):
        self.assertNotIn('"', S.esc('議案「"x"」'))


if __name__ == "__main__":
    unittest.main()
