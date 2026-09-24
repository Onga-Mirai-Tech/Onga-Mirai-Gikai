"""議案を会議録から組み立てる。

1つの議案は**同じ定例会の複数の日にまたがる**。
令和8年第3回定例会の議案第44号なら、6月8日に質疑と委員会付託、6月15日に採決。
そのため日ごとではなく会議ごとに組む。

進行は定型文で進むので、それを手がかりにする（全460件で確認）。

    △日程第N          … 議事日程の行から議案番号と件名が引ける
    これより、議案質疑に入ります
    これより、委員会付託に入ります  →「第一常任委員会に付託致します」/「委員会付託を省略」
    これより、討論に入ります
    これより、採決に入ります       → ───　賛成者起立　─── / ───　異議なしの声　───
    よって、本案は…可決されました  → 議決結果

議決結果の言い回しは342通りある。結果語が拾えなかったものは `null` にして
verify工程で町HPの「審議案件・結果」PDFと突き合わせる。推測では埋めない。

    python -m pipeline.parse.bills            data/normalized/bills.json を書く
    python -m pipeline.parse.bills --report   要約だけ表示する
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from pipeline.common.http import decode_cp932
from pipeline.fetch.voices import unid_to_date
from pipeline.parse import transcript as T

# 議案の種別。第N号の前に付く語（全460件から採った）。
KINDS = ("議案", "発議", "発委", "報告", "意見書案", "決議案", "請願", "陳情", "選挙", "推薦", "指定", "承認", "諮問")
_NUM_RE = re.compile(rf"({'|'.join(KINDS)})第\s*([０-９0-9]+)\s*号")
_AGENDA_NO_RE = re.compile(r"^日程第\s*([０-９0-9]+)")
_TITLE_RE = re.compile(r"「([^」]{4,200})」")

# 進行の定型文。表記ゆれ（「致します」「いたします」、読点の全角差）を吸収する。
_STAGE_MARKERS = [
    ("質疑", re.compile(r"これより[、，]?\s*議案質疑に入ります")),
    ("委員会付託", re.compile(r"これより[、，]?\s*委員会付託に入ります")),
    ("討論", re.compile(r"これより[、，]?\s*討論に入ります")),
    ("採決", re.compile(r"これより[、，]?\s*採決に入ります")),
    ("提案理由", re.compile(r"提案理由の説明")),
    ("委員長報告", re.compile(r"委員長報告を求めます")),
]

# 「それぞれ所管ごとに第一・第二常任委員会に付託致します」のように、
# 常任委員会を中黒で並べて分割付託する形がある。両方を拾う。
_REFERRAL_RE = re.compile(
    r"((?:第[一二三四五六七八九十]+[・､、]?)+常任委員会|議会運営委員会|[^、。\s　]{2,25}特別委員会)(?:に|へ)?付託"
)
_COMMITTEE_SPLIT_RE = re.compile(r"第([一二三四五六七八九十]+)")


def split_committees(matched: str) -> list[str]:
    """「第一・第二常任委員会」→ ["第一常任委員会", "第二常任委員会"]"""
    if "常任委員会" not in matched:
        return [matched]
    nums = _COMMITTEE_SPLIT_RE.findall(matched)
    return [f"第{n}常任委員会" for n in nums] or [matched]
_REFERRAL_OMIT_RE = re.compile(r"委員会付託を省略")
_TALLY_RE = re.compile(r"([０-９0-9]+)\s*[：:]\s*([０-９0-9]+)\s*〔\s*([０-９0-9]+)\s*〕")

_RESULT_WORDS = (
    "修正可決", "原案可決", "可決", "否決", "承認", "不承認", "同意", "不同意",
    "認定", "不認定", "採択", "不採択", "継続審査", "継続調査", "適任",
)
_RESULT_SENT_RE = re.compile(r"よって[、，]?[^。]{0,80}")
_RESULT_WORD_RE = re.compile("|".join(_RESULT_WORDS))

# 「令和元年」の「元」は数字ではない。会議録がそう書いているので、そのまま受ける。
# `\d+` だけにすると令和元年の会議IDが作れず、IDに会議名がそのまま入ってしまう。
_MEETING_RE = re.compile(r"(令和|平成|昭和)(元|\d+)年第(\d+)回(定例会|臨時会)")
_ERA_BASE = {"昭和": 1925, "平成": 1988, "令和": 2018}


def meeting_id(title_raw: str) -> tuple[str, str]:
    """「令和　８年第　３回定例会」→ ("2026-t3", "令和8年第3回定例会")"""
    name = T.squeeze(title_raw)
    m = _MEETING_RE.search(name)
    if not m:
        return name, name
    era, yy, no, kind = m.groups()
    year = _ERA_BASE[era] + (1 if yy == "元" else int(yy))
    return f"{year}-{'t' if kind == '定例会' else 'r'}{no}", name


def bill_number(kind: str, no: str) -> str:
    return f"{kind}第{T.squeeze(no)}号"


@dataclass
class Stage:
    kind: str
    on: str
    unid: str
    agenda_no: int | None
    seq: int


@dataclass
class Vote:
    method: str  # 起立 | 異議なし | 記載なし
    yes: int | None = None
    no: int | None = None
    present: int | None = None


@dataclass
class Bill:
    id: str
    meeting_id: str
    meeting: str
    number: str
    kind: str
    title: str = ""
    submitted_on: str = ""
    decided_on: str = ""
    result: str | None = None
    vote: Vote | None = None
    referral_status: str = "none"  # committee | omitted | none
    committees: list[str] = field(default_factory=list)
    referred_on: str = ""
    stages: list[Stage] = field(default_factory=list)
    utterances: list[dict] = field(default_factory=list)
    # 質疑・討論に実体があったか。要約を作るかどうかの判断に使う。
    # True=あり / False=なし（「───　質疑なし　───」）/ 欠けている=段階自体がない
    content: dict[str, bool] = field(default_factory=dict)

    @property
    def has_discussion(self) -> bool:
        """質疑または討論に実体があるか。これが False の議案は要約を作らない。"""
        return any(self.content.get(k) for k in ("質疑", "討論"))


def _mentions(text: str, number: str) -> bool:
    """その文が、この議案番号を名指ししているか。

    一括議題では1つの発言に複数の議案番号が出るので、
    どの議案の話かを番号で見分ける。
    """
    return number in T.squeeze(text)


def _agenda_map(tr: T.Transcript) -> dict[int, tuple[str, str]]:
    """議事日程の行から 日程番号 → (議案番号, 件名) を作る。"""
    out: dict[int, tuple[str, str]] = {}
    for line in tr.agenda:
        mn = _AGENDA_NO_RE.match(T.squeeze(line))
        mnum = _NUM_RE.search(line)
        if not mn or not mnum:
            continue
        title = ""
        mt = _TITLE_RE.search(line)
        if mt:
            title = mt.group(1).strip()
        else:
            tail = line[mnum.end():].strip("　 ")
            title = re.sub(r"〔[^〕]*〕", "", tail).strip("　 ")
        out[int(mn.group(1))] = (bill_number(mnum.group(1), mnum.group(2)), title)
    return out


# 「議案第50号から、／議案第59号までを、一括して議題と致します」
# 決算認定は必ずこの形で10件まとめて処理される。範囲を展開しないと、
# 委員長報告・討論・採決のすべてを取りこぼす（実測: 令和分で55件）。
_RANGE_RE = re.compile(
    r"(議案|報告|発議|発委|意見書案|請願|陳情|諮問|承認)第\s*([０-９0-9]+)\s*号から[、，]?\s*"
    r"(?:(?:議案|報告|発議|発委|意見書案|請願|陳情|諮問|承認)第\s*)?([０-９0-9]+)\s*号まで"
)


def _range_numbers(text: str, amap: dict[int, tuple[str, str]]) -> list[str]:
    """一括議題の範囲を議案番号の並びに開く。

    **議事日程に載っている番号だけ**を返す。範囲の間に欠番があることがあり
    （撤回・不上程）、機械的に埋めると存在しない議案を作ってしまう。
    """
    m = _RANGE_RE.search(T.squeeze(text))
    if not m:
        return []
    kind, lo, hi = m.group(1), int(T.squeeze(m.group(2))), int(T.squeeze(m.group(3)))
    if hi < lo or hi - lo > 60:
        return []
    known = {num for num, _title in amap.values()}
    return [n for n in (bill_number(kind, str(i)) for i in range(lo, hi + 1)) if n in known]


def extract_day(tr: T.Transcript, on: date) -> dict[str, dict]:
    """1日分から、議案番号ごとの出来事を拾う。"""
    amap = _agenda_map(tr)
    events: dict[str, dict] = collections.defaultdict(
        lambda: {"stages": [], "utterances": [], "title": "", "referral": None,
                 "committees": [], "referred_on": "", "vote": None, "result": None,
                 "content": {}}
    )
    currents: list[str] = []
    agenda_no: int | None = None
    pending: str | None = None
    utterances = list(tr.utterances)
    # 範囲指定の後半（「議案第59号までを、…」）を日程行として読み直さないための印。
    # そのまま読むと、一括議題が最後の1件だけの議題にすり替わる。
    consumed = -1

    for i, u in enumerate(utterances):
        if u.kind in ("日程", "追加日程") and i != consumed:
            agenda_no = u.agenda_no
            pending = None
            # 範囲指定は2つの日程行に割れることがある（「…から、」「…までを、」）。
            # 次の日程行までをつないで見る。
            joined = u.text
            nxt = i + 1 < len(utterances) and utterances[i + 1].kind in ("日程", "追加日程")
            if nxt:
                joined += utterances[i + 1].text
            ranged = _range_numbers(joined, amap)
            if ranged and nxt and not _range_numbers(u.text, amap):
                consumed = i + 1  # 後半は範囲の一部。単独の日程として読まない。

            mapped = amap.get(u.agenda_no or -1)
            in_body = _NUM_RE.search(u.text)
            if ranged:
                currents = ranged
            elif mapped:
                currents = [mapped[0]]
                events[mapped[0]]["title"] = events[mapped[0]]["title"] or mapped[1]
            elif in_body:
                currents = [bill_number(in_body.group(1), in_body.group(2))]
            else:
                currents = []  # 会期の決定・一般質問など、議案でない日程

            for number in currents:
                ev = events[number]
                ev["stages"].append(Stage("上程", str(on), tr.unid, agenda_no, u.seq))
                if not ev["title"]:
                    mt = _TITLE_RE.search(u.text)
                    if mt:
                        ev["title"] = mt.group(1).strip()
                    elif number in {n for n, _ in amap.values()}:
                        ev["title"] = next(ti for n, ti in amap.values() if n == number)

        if not currents:
            continue

        text = u.text
        for number in currents:
            events[number]["utterances"].append(
                {"unid": tr.unid, "seq": u.seq, "huid": u.huid})

        # 段階・質疑討論の有無・付託は、一括議題なら全件に等しくかかる。
        stages_here = [kind for kind, pat in _STAGE_MARKERS if pat.search(text)]
        for kind in stages_here:
            for number in currents:
                events[number]["stages"].append(Stage(kind, str(on), tr.unid, agenda_no, u.seq))
            if kind in ("質疑", "討論"):
                pending = kind

        # 「───　質疑なし　───」のト書きが出れば実体なし。
        # 質問者・答弁者の発言が続けば実体あり。
        notes_joined = "".join(u.notes)
        if pending:
            # 「討論は、ございませんか」は問いかけであって不在の宣言ではない。
            # 判定はト書き（───　討論なし　───）だけに頼る。
            if f"{pending}なし" in notes_joined:
                for number in currents:
                    events[number]["content"][pending] = False
                pending = None
            elif u.kind in ("質問者", "答弁者"):
                for number in currents:
                    events[number]["content"][pending] = True
                pending = None
            elif any(p.search(text) for _k, p in _STAGE_MARKERS if _k not in (pending,)):
                pending = None

        if _REFERRAL_OMIT_RE.search(text):
            for number in currents:
                events[number]["referral"] = "omitted"
                events[number]["referred_on"] = events[number]["referred_on"] or str(on)
        for m in _REFERRAL_RE.finditer(text):
            for number in currents:
                ev = events[number]
                ev["referral"] = "committee"
                ev["referred_on"] = ev["referred_on"] or str(on)
                for c in split_committees(m.group(1)):
                    if c not in ev["committees"]:
                        ev["committees"].append(c)

        # 採決と議決結果は、一括議題でも**議案ごとに**宣言される。
        # 「よって、議案第50号…は、認定することに決しました。続きまして、議案第51号…」
        # 全件に同じ結果を配ると、1件だけ否決された場合に取り違える。
        named = [n for n in currents if _mentions(text, n)]
        targets = named or currents

        if "賛成者起立" in notes_joined:
            mt = _TALLY_RE.search(text)
            vote = (Vote("起立", int(T.squeeze(mt.group(1))), int(T.squeeze(mt.group(2))),
                         int(T.squeeze(mt.group(3)))) if mt else Vote("起立"))
            for number in targets:
                if events[number]["vote"] is None:
                    events[number]["vote"] = vote
        elif "異議なし" in notes_joined:
            for number in targets:
                if events[number]["vote"] is None:
                    events[number]["vote"] = Vote("異議なし")

        for sent in _RESULT_SENT_RE.findall(text):
            mw = _RESULT_WORD_RE.search(sent)
            if not mw:
                continue
            in_sent = [n for n in currents if _mentions(sent, n)]
            for number in (in_sent or targets):
                ev = events[number]
                if ev["result"] is None:
                    ev["result"] = mw.group(0)
                    ev["stages"].append(Stage("議決", str(on), tr.unid, agenda_no, u.seq))

    return events


def build(paths: list[Path]) -> list[Bill]:
    bills: dict[tuple[str, str], Bill] = {}
    for f in sorted(paths):
        tr = T.parse(decode_cp932(f.read_bytes()), f.stem)
        mid, mname = meeting_id(tr.title_raw)
        on = unid_to_date(f.stem)
        for number, ev in extract_day(tr, on).items():
            key = (mid, number)
            b = bills.get(key)
            if b is None:
                kind = next((k for k in KINDS if number.startswith(k)), "議案")
                b = Bill(id=f"{mid}-b{number}", meeting_id=mid, meeting=mname,
                         number=number, kind=kind, submitted_on=str(on))
                bills[key] = b
            b.title = b.title or ev["title"]
            b.stages.extend(ev["stages"])
            b.utterances.extend(ev["utterances"])
            if ev["referral"] and b.referral_status == "none":
                b.referral_status = ev["referral"]
                b.referred_on = ev["referred_on"]
            for c in ev["committees"]:
                if c not in b.committees:
                    b.committees.append(c)
            if ev["vote"] and b.vote is None:
                b.vote = ev["vote"]
            for k, v in ev["content"].items():
                b.content.setdefault(k, v)
            if ev["result"] and b.result is None:
                b.result = ev["result"]
                b.decided_on = str(on)

    return sorted(bills.values(), key=lambda b: (b.submitted_on, b.number))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="会議録から議案を組み立てる")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[2]
    files = sorted((root / "data" / "raw" / "voices").glob("K_*.html"))
    if not files:
        print("data/raw/voices に会議録がありません。", file=sys.stderr)
        return 1

    bills = build(files)
    with_result = sum(1 for b in bills if b.result)
    with_vote = sum(1 for b in bills if b.vote)
    referred = sum(1 for b in bills if b.referral_status == "committee")
    omitted = sum(1 for b in bills if b.referral_status == "omitted")

    print(f"議案 {len(bills)}件")
    print(f"  議決結果あり : {with_result}（{with_result/len(bills)*100:.1f}%）")
    print(f"  採決の記載あり: {with_vote}")
    print(f"  委員会付託    : {referred}　付託省略: {omitted}")
    print("  種別:", dict(collections.Counter(b.kind for b in bills).most_common()))
    print("  結果:", dict(collections.Counter(b.result for b in bills if b.result).most_common(8)))

    if args.report:
        return 0

    out = root / "data" / "normalized" / "bills.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps([_as_dict(b) for b in bills], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\n→ {out.relative_to(root)}")
    return 0


def _as_dict(b: Bill) -> dict:
    d = asdict(b)
    d["vote"] = asdict(b.vote) if b.vote else None
    return d


if __name__ == "__main__":
    raise SystemExit(main())
