"""一覧パーサのテスト。

原文HTMLは private の data リポジトリにあり、public 側のCIからは見えない。
そのため、ここでは**構造だけを真似た合成データ**を使う。
実物での検証は `data/raw/_p0/` の保存済みHTMLに対して手で行う（P0で実施済み）。

    python -m unittest discover -s pipeline/tests
"""

from __future__ import annotations

import unittest
from datetime import date

from pipeline.fetch.town import label_to_year, parse_attachments, parse_year_pages
from pipeline.fetch.voices import (
    normalize,
    parse_hit_count,
    parse_schedule_list,
    parse_issue_label,
    unid_to_date,
)

# VOICES の一覧の1行（ACT=100 の応答）を模したもの。
# 実物は Shift_JIS・大文字タグ・&エスケープなしで、会議名が全角スペースで字送りされている。
VOICES_LIST = """
<TABLE><TR><TD COLSPAN=2>375件の日程がヒットしました。</TD></TR></TABLE>
<TABLE>
<TR><TD NOWRAP>
<A HREF="voiweb.exe?ACT=100&FINO=457"><IMG SRC="/voices/image/folder.gif"></A>
令和　８年第　３回定例会,<A HREF="voiweb.exe?ACT=200&KGTP=1&KGNO=167&FINO=457&UNID=K_R08060400011" TARGET="HLD_WIN">06月04日-01号</A>
</TD></TR>
<TR><TD NOWRAP>
<A HREF="voiweb.exe?ACT=100&FINO=458"><IMG SRC="/voices/image/folder.gif"></A>
令和　８年第　３回定例会,<A HREF="voiweb.exe?ACT=200&amp;KGTP=1&amp;KGNO=167&amp;FINO=458&amp;UNID=K_R08060800021" TARGET="HLD_WIN">06月08日-02号</A>
</TD></TR>
<TR><TD NOWRAP>
<A HREF="voiweb.exe?ACT=100&FINO=39"><IMG SRC="/voices/image/folder.gif"></A>
平成１８年第　６回定例会,<A HREF="voiweb.exe?ACT=200&KGTP=1&KGNO=15&FINO=39&UNID=K_H18120400011" TARGET="HLD_WIN">12月04日-01号</A>
</TD></TR>
</TABLE>
"""

TOWN_INDEX = """
<div id="main">
<a href="/site/gikai/83670.html">一般質問通告書（令和8年）</a>
<a href="/site/gikai/6562.html">一般質問通告書（令和元年）</a>
<a href="/site/gikai/6563.html">一般質問通告書（平成30年）</a>
<a href="/site/gikai/list20-65.html">審議案件・結果</a>
</div>
"""

TOWN_YEAR_PAGE = """
<div id="main">
<a href="/uploaded/attachment/12465.pdf">令和8年9月 [PDFファイル／263KB]</a>
<a href="/uploaded/attachment/12067.pdf">令和8年6月 [PDFファイル／182KB]</a>
<a href="/uploaded/attachment/9346.pdf">&#8203;</a>
<a href="https://get.adobe.com/reader/">Adobe Reader</a>
</div>
"""


class TestVoices(unittest.TestCase):
    def test_unid_to_date(self):
        self.assertEqual(unid_to_date("K_R08060400011"), date(2026, 6, 4))
        self.assertEqual(unid_to_date("K_H18061600031"), date(2006, 6, 16))
        # 平成31年と令和元年が同じ2019年にまたがるが、UNIDの元号で決まる
        self.assertEqual(unid_to_date("K_H31040100011"), date(2019, 4, 1))
        self.assertEqual(unid_to_date("K_R01050700011"), date(2019, 5, 7))

    def test_unid_to_date_rejects_unknown_shape(self):
        with self.assertRaises(ValueError):
            unid_to_date("R08060400011")

    def test_parse_issue_label(self):
        self.assertEqual(parse_issue_label("06月08日-02号"), (6, 8, 2))
        self.assertEqual(parse_issue_label("12月04日-01号"), (12, 4, 1))

    def test_issue_no_comes_from_label_not_unid(self):
        # 臨時会の UNID は号の桁が 0101 になっている。数値にすると101号になるため、
        # 号は表示ラベルから取る。
        self.assertEqual(parse_issue_label("01月22日-01号"), (1, 22, 1))

    def test_normalize_strips_fullwidth_padding(self):
        self.assertEqual(normalize("令和　８年第　３回定例会"), "令和8年第3回定例会")
        self.assertEqual(normalize("平成１８年第　６回定例会"), "平成18年第6回定例会")

    def test_parse_hit_count(self):
        self.assertEqual(parse_hit_count(VOICES_LIST), 375)
        self.assertIsNone(parse_hit_count("<html></html>"))

    def test_parse_schedule_list(self):
        schedules = parse_schedule_list(VOICES_LIST, kgtp=1)
        self.assertEqual(len(schedules), 3)

        first = schedules[0]
        self.assertEqual(first.unid, "K_R08060400011")
        self.assertEqual(first.fino, 457)
        self.assertEqual(first.kgno, 167)
        self.assertEqual(first.meeting_name, "令和8年第3回定例会")
        self.assertEqual(first.held_on, date(2026, 6, 4))
        self.assertEqual(first.issue_no, 1)
        self.assertEqual(first.filename, "K_R08060400011.html")
        self.assertIn("ACT=203", first.body_url)
        self.assertIn("FINO=457", first.body_url)

    def test_parse_schedule_list_handles_escaped_ampersands(self):
        # 実物は素の & と &amp; が混在する
        second = parse_schedule_list(VOICES_LIST, kgtp=1)[1]
        self.assertEqual(second.fino, 458)
        self.assertEqual(second.issue_no, 2)

    def test_parse_schedule_list_dedupes(self):
        doubled = VOICES_LIST + VOICES_LIST
        self.assertEqual(len(parse_schedule_list(doubled, kgtp=1)), 3)


class TestTown(unittest.TestCase):
    def test_label_to_year(self):
        self.assertEqual(label_to_year("一般質問通告書（令和8年）"), 2026)
        self.assertEqual(label_to_year("一般質問通告書（令和元年）"), 2019)
        self.assertEqual(label_to_year("一般質問通告書（平成30年）"), 2018)
        self.assertEqual(label_to_year("バックナンバー"), 0)

    def test_parse_year_pages_sorted_newest_first(self):
        pages = parse_year_pages(TOWN_INDEX, "一般質問通告書")
        labels = [label for _url, label in pages]
        self.assertEqual(
            labels,
            [
                "一般質問通告書（令和8年）",
                "一般質問通告書（令和元年）",
                "一般質問通告書（平成30年）",
            ],
        )
        # 文字列順だと「平成30年」が先頭に来てしまう。最新年の取り直し判定が壊れる。
        self.assertNotEqual(labels[0], "一般質問通告書（平成30年）")

    def test_parse_year_pages_ignores_other_sections(self):
        pages = parse_year_pages(TOWN_INDEX, "一般質問通告書")
        self.assertTrue(all("list20-65" not in url for url, _ in pages))

    def test_parse_attachments(self):
        attachments = parse_attachments(TOWN_YEAR_PAGE, "一般質問通告書（令和8年）")
        self.assertEqual(len(attachments), 3)
        names = [a.filename for a in attachments]
        self.assertIn("12067_令和8年6月.pdf", names)
        self.assertIn("12465_令和8年9月.pdf", names)

    def test_parse_attachments_falls_back_for_empty_label(self):
        # ラベルがゼロ幅スペースだけのリンクが実在する。取りこぼさず名前を作る。
        attachments = parse_attachments(TOWN_YEAR_PAGE, "一般質問通告書（令和8年）")
        stray = next(a for a in attachments if a.attachment_id == "9346")
        self.assertTrue(stray.filename.startswith("9346_"))
        self.assertTrue(stray.filename.endswith(".pdf"))

    def test_parse_attachments_skips_external_links(self):
        attachments = parse_attachments(TOWN_YEAR_PAGE, "令和8年")
        self.assertTrue(all(a.url.startswith("https://www.town.onga.lg.jp/") for a in attachments))


if __name__ == "__main__":
    unittest.main()
