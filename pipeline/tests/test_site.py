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


class TestMemberSlug(unittest.TestCase):
    def site(self, slugs):
        return S.Site(out=None, transcripts={}, fino={}, threads=[], bills=[],
                      speakers={}, meetings={}, slugs=slugs, member_bills={},
                      current_members=set())

    def test_uses_romaji_when_given(self):
        # 氏名の読みはこちらでは決められないので、人が masters/speakers.json に入れる
        self.assertEqual(S.member_slug(self.site({"野口久美子": "noguchi-kumiko"}), "野口久美子"),
                         "noguchi-kumiko")

    def test_falls_back_to_the_name(self):
        self.assertEqual(S.member_slug(self.site({}), "野口久美子"), "野口久美子")


class TestMemberRole(unittest.TestCase):
    def test_member_titles(self):
        for t in ("３番議員", "１１番議員", "議長", "副議長", "第一常任委員会委員長"):
            self.assertTrue(S.is_member_role(t), t)

    def test_executive_titles_are_excluded(self):
        # 議員から町長になった人の、町長としての答弁は議員ページに載せない
        for t in ("町長", "副町長", "教育長", "税務課長", "会計管理者"):
            self.assertFalse(S.is_member_role(t), t)


class TestMembersInScope(unittest.TestCase):
    def test_includes_a_former_member_who_became_mayor(self):
        # 古野修：議員 → 議長 → 町長。議員ページは持つ。
        site = S.Site(out=None, transcripts={}, fino={}, threads=[], bills=[],
                      speakers={
                          "古野修": {"name": "古野修", "kind": "町長", "seats": ["３番"],
                                   "first": "2008-03-07", "last": "2026-06-09"},
                          "井口正彦": {"name": "井口正彦", "kind": "執行部", "seats": [],
                                    "first": "2020-01-01", "last": "2026-06-09"},
                      },
                      meetings={}, slugs={}, member_bills={}, current_members=set())
        self.assertEqual([m["name"] for m in S.members_in_scope(site)], ["古野修"])


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
