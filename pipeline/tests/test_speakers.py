"""発言者マスタのテスト。

実物で確かめた事実を固定する。

- 同じ人が時代で役職を変える（議員 → 議長 → 町長）
- 臨時会では説明員欄にその職の記載がないことがある
- 表記ゆれは**自動で統合しない**。候補を添えて一覧に出すだけ

    python -m unittest discover -s pipeline/tests -t .
"""

from __future__ import annotations

import unittest
from datetime import date

from pipeline.parse import speakers as S
from pipeline.parse import transcript as T
from pipeline.tests.test_transcript import build

HEADER = """令和　８年第　３回定例会<BR><BR>１．議長の氏名　　　織田　隆徳<BR><BR>
２．説明のため出席した者の氏名・職<BR><BR>
　　　町長　　　　　古野　　修<BR>
　　　税務課長　　　井口　正彦<BR><BR>
３．書記の氏名<BR><BR>
　　　議会事務局長　野口　健治<BR><BR>
４．議員の出欠　（出席 ／・　欠席　△）<BR><BR>
│ ／ │１番　│仲　摩　靖　浩││ ／ │３番　│田　代　順　二│<BR>"""

# 臨時会。説明員欄に税務課長の記載がない。
HEADER_RINJI = """令和　８年第　１回臨時会<BR><BR>１．議長の氏名　　　織田　隆徳<BR><BR>
２．説明のため出席した者の氏名・職<BR><BR>
　　　町長　　　　　古野　　修<BR><BR>
３．書記の氏名<BR><BR>
　　　議会事務局長　野口　健治<BR><BR>
４．議員の出欠　（出席 ／・　欠席　△）<BR><BR>
│ ／ │１番　│仲　摩　靖　浩│<BR>"""

NO_OVERRIDES = {"aliases": {}, "external": [], "slugs": {}, "notes": {}}


def parse(header: str, *blocks: str) -> T.Transcript:
    return T.parse(build(header, *blocks), "K_R08060400011")


class TestObserve(unittest.TestCase):
    def observe(self, tr, roster_all=None, **kw):
        o = dict(aliases={}, external=set())
        o.update(kw)
        return S.observe(tr, date(2026, 6, 4), o["aliases"], o["external"], roster_all)

    def test_seat_match(self):
        tr = parse(HEADER, "◆３番議員（田代順二）　質問します。")
        self.assertEqual(self.observe(tr)[0].match, S.MATCH_SEAT)

    def test_roster_match_for_executive(self):
        tr = parse(HEADER, "◎税務課長（井口正彦）　お答えします。")
        self.assertEqual(self.observe(tr)[0].match, S.MATCH_ROSTER)

    def test_chair_matches_header(self):
        tr = parse(HEADER, "○議長（織田隆徳）　開会します。")
        self.assertEqual(self.observe(tr)[0].match, S.MATCH_ROSTER)

    def test_executive_absent_from_that_day_uses_full_roster(self):
        # 臨時会では説明員欄にその職が載らないことがある。当日のヘッダだけで
        # 照合すると、実在する課長が未照合に落ちる。
        tr = parse(HEADER_RINJI, "◎税務課長（井口正彦）　お答えします。")
        self.assertEqual(self.observe(tr)[0].match, S.MATCH_NONE)
        self.assertEqual(
            self.observe(tr, roster_all={"井口正彦": {"税務課長"}})[0].match,
            S.MATCH_ROSTER_OTHER,
        )

    def test_guest_is_external_not_unresolved(self):
        # 代表監査委員・人権擁護委員・受章者など、名簿に載らない立場
        tr = parse(HEADER, "◎代表監査委員（矢野榮治）　報告します。", "◎濱之上喜郎氏　ありがとうございました。")
        self.assertEqual([o.match for o in self.observe(tr)], [S.MATCH_EXTERNAL, S.MATCH_EXTERNAL])

    def test_unknown_member_is_unresolved(self):
        # 議席ラベルなのに出欠表にいない＝表記ゆれの疑い。人が判断する。
        tr = parse(HEADER, "◆１番議員（仲摩靖弘）　質問します。")
        self.assertEqual(self.observe(tr)[0].match, S.MATCH_NONE)

    def test_alias_resolves_unmatched_name(self):
        tr = parse(HEADER, "◆１番議員（仲摩靖弘）　質問します。")
        got = self.observe(tr, aliases={"仲摩靖弘": "仲摩靖浩"})[0]
        self.assertEqual(got.match, S.MATCH_SEAT)
        self.assertEqual(got.name, "仲摩靖浩")

    def test_alias_applies_to_the_roster_too(self):
        # 会議録は出欠表の中でも表記が揺れている（平見光司274回／平見光二24回）。
        # 発言ラベルだけ寄せて名簿を放置すると、逆に照合できなくなる。
        header = HEADER.replace("仲　摩　靖　浩", "平　見　光　二")
        tr = parse(header, "◆１番議員（平見光司）　質問します。")
        self.assertEqual(self.observe(tr)[0].match, S.MATCH_NONE)
        got = self.observe(tr, aliases={"平見光二": "平見光司"})[0]
        self.assertEqual(got.match, S.MATCH_SEAT)


class TestClassify(unittest.TestCase):
    def role(self, title, last):
        return S.Role(title=title, count=1, first="2005-01-01", last=last)

    def test_latest_role_wins(self):
        # 古野修：3番議員 → 11番議員 → 議長 → 町長。現職は町長。
        roles = [
            self.role("３番議員", "2011-03-07"),
            self.role("１１番議員", "2014-12-16"),
            self.role("議長", "2018-09-19"),
            self.role("町長", "2026-06-09"),
        ]
        self.assertEqual(S.classify(roles), "町長")

    def test_sitting_chair_is_still_a_member(self):
        roles = [self.role("１１番議員", "2020-01-01"), self.role("議長", "2026-06-15")]
        self.assertEqual(S.classify(roles), "議員")

    def test_executive(self):
        self.assertEqual(S.classify([self.role("税務課長", "2026-06-09")]), "執行部")

    def test_renamed_post(self):
        # 助役 → 副町長 → 町長（地方自治法の改正で助役が副町長になった）
        roles = [
            self.role("助役", "2007-03-08"),
            self.role("副町長", "2010-11-12"),
            self.role("町長", "2018-12-05"),
        ]
        self.assertEqual(S.classify(roles), "町長")


class TestBuild(unittest.TestCase):
    def make(self, *obs) -> tuple[list[S.Speaker], list[dict]]:
        return S.build(list(obs), NO_OVERRIDES)

    def ob(self, name, title, on, match=S.MATCH_SEAT, raw=None):
        return S.Observation(name, raw or name, title, "K_R08060400011", on, 1, match)

    def test_roles_carry_date_ranges(self):
        speakers, _ = self.make(
            self.ob("古野修", "３番議員", date(2008, 3, 7)),
            self.ob("古野修", "３番議員", date(2011, 3, 7)),
            self.ob("古野修", "町長", date(2026, 6, 9), S.MATCH_ROSTER),
        )
        s = speakers[0]
        self.assertEqual(s.name, "古野修")
        self.assertEqual(s.kind, "町長")
        by_title = {r.title: r for r in s.roles}
        self.assertEqual((by_title["３番議員"].first, by_title["３番議員"].last), ("2008-03-07", "2011-03-07"))
        self.assertEqual(by_title["３番議員"].count, 2)
        self.assertEqual(s.seats, ["３番"])

    def test_unresolved_lists_similar_names_but_does_not_merge(self):
        speakers, unresolved = self.make(
            self.ob("平見光二", "１番議員", date(2005, 3, 4)),
            self.ob("平見光司", "１番議員", date(2005, 3, 8), S.MATCH_NONE),
        )
        # 2人のまま。自動では統合しない。
        self.assertEqual({s.name for s in speakers}, {"平見光二", "平見光司"})
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["name"], "平見光司")
        self.assertEqual([c["name"] for c in unresolved[0]["候補"]], ["平見光二"])

    def test_report_says_nothing_unresolved(self):
        speakers, unresolved = self.make(self.ob("仲摩靖浩", "１番議員", date(2026, 6, 4)))
        self.assertIn("未判定はない", S.render_report(speakers, unresolved, 1))

    def test_slug_override_shapes_id(self):
        overrides = dict(NO_OVERRIDES, slugs={"野口久美子": "noguchi-kumiko"})
        speakers, _ = S.build([self.ob("野口久美子", "２番議員", date(2026, 6, 9))], overrides)
        self.assertEqual(speakers[0].id, "sp-noguchi-kumiko")


if __name__ == "__main__":
    unittest.main()
