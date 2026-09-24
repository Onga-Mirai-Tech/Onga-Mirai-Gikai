"""一般質問をスレッドに分ける。

1人の議員の一般質問を1スレッドとする。要約は**質問事項ごと**に作るので、
スレッドの中を通告書（`data/raw/tsukokusho/`）の質問事項で割る。

区切りは文言ではなく**構造**で取る。「以上で、◯◯議員の一般質問は終了致しました」
という締めの文言は後年しか使われておらず（154日中67日で不在）、これに頼ると
平成期がまるごと落ちる。

    開始 … 「一般質問を許します」を含む発言、または議事日程が一般質問の △日程第N
    終了 … 次の日程の見出し
    区切り … ◆質問者が替わったところ

**追加日程は一般質問ではない。** 令和5年12月11日には緊急動議の特別委員会設置が
追加日程第1にあり、日程番号だけで照合すると、その質疑応答を一般質問として
拾ってしまう。

    python -m pipeline.parse.threads            data/normalized/threads.json を書く
    python -m pipeline.parse.threads --report   要約だけ表示する
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
from pipeline.parse.bills import meeting_id

_OPENER_RE = re.compile(r"一般質問を許します")
_AGENDA_NO_RE = re.compile(r"^日程第(\d+)")
# 議長が次の質問者を指名する行。スレッドの頭を確かめるのに使う。
_CALL_RE = re.compile(r"([０-９0-9]{1,2}番議員[、，]?\s*)?([^\s　、。]{2,10})\s*(?:議員|君|さん)[。、，]?\s*$")


@dataclass
class Turn:
    seq: int
    huid: int
    kind: str  # 質問者 | 答弁者 | 議長
    title: str
    name: str


@dataclass
class Thread:
    id: str
    meeting_id: str
    meeting: str
    unid: str
    on: str
    order: int  # その日の通告順（1から）
    questioner: str
    questioner_title: str
    responders: list[str] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    topics: list[dict] = field(default_factory=list)  # 通告書の質問事項（P2の後段で入れる）
    note: str = ""

    @property
    def chars(self) -> int:
        return sum(len(t.name) for t in self.turns)


def question_agenda_nos(tr: T.Transcript) -> set[int]:
    """議事日程の行が「一般質問」になっている日程番号。"""
    out: set[int] = set()
    for line in tr.agenda:
        s = T.squeeze(line)
        m = _AGENDA_NO_RE.match(s)
        if m and "一般質問" in s:
            out.add(int(m.group(1)))
    return out


def find_section(tr: T.Transcript) -> tuple[int, int] | None:
    """一般質問の区間を (開始seq, 終了seq) で返す。無ければ None。

    開始は「一般質問を許します」を優先する。議事日程には一般質問と書いてあるのに
    本文に日程の見出しが1つも無い日があるため（平成24年3月8日）。
    """
    nos = question_agenda_nos(tr)
    start = None
    for u in tr.utterances:
        if _OPENER_RE.search(u.text):
            start = u.seq
            break
        if u.kind == "日程" and u.agenda_no in nos:
            # 追加日程は対象外。日程番号が同じでも別物（緊急動議など）。
            start = u.seq
            break
    if start is None:
        return None

    end = tr.utterances[-1].seq
    for u in tr.utterances:
        if u.seq > start and u.kind in ("日程", "追加日程"):
            end = u.seq - 1
            break
    return start, end


def split_threads(tr: T.Transcript, on: date) -> list[Thread]:
    """1日分から一般質問のスレッドを取り出す。"""
    section = find_section(tr)
    if section is None:
        return []
    start, end = section
    mid, mname = meeting_id(tr.title_raw)

    inside = [u for u in tr.utterances if start <= u.seq <= end]

    # 質問者ごとの、区間内での最初と最後の発言位置
    span: dict[str, list[int]] = {}
    title_of: dict[str, str] = {}
    for u in inside:
        if u.kind != "質問者" or not u.name:
            continue
        if u.name in span:
            span[u.name][1] = u.seq
        else:
            span[u.name] = [u.seq, u.seq]
            title_of[u.name] = u.title

    # 長い一般質問の途中に別の議員が割り込むことがある（議事進行など）。
    # 他の議員の区間にすっぽり収まる質問者は、独立したスレッドにしない。
    owners = sorted(span, key=lambda n: span[n][0])
    nested: dict[str, str] = {}
    for name in owners:
        a, b = span[name]
        for other in owners:
            if other == name:
                continue
            c, d = span[other]
            if c < a and b < d:
                nested[name] = other
                break

    threads: list[Thread] = []
    current: Thread | None = None
    for u in inside:
        if u.kind == "質問者" and u.name and u.name not in nested:
            if current is None or current.questioner != u.name:
                current = Thread(
                    id=f"{mid}-q{len(threads) + 1:02d}-{tr.unid}",
                    meeting_id=mid, meeting=mname, unid=tr.unid, on=str(on),
                    order=len(threads) + 1,
                    questioner=u.name, questioner_title=title_of[u.name],
                )
                threads.append(current)
        if current is None:
            continue  # 最初の質問者が現れる前の議長の前口上
        current.turns.append(Turn(u.seq, u.huid, u.kind, u.title, u.name))
        if u.kind == "答弁者" and u.name and u.name not in current.responders:
            current.responders.append(u.name)

    for name, owner in nested.items():
        for t in threads:
            if t.questioner == owner:
                t.note = f"{name}議員の発言が途中に挟まっている（議事進行など）"

    seen = [t.questioner for t in threads]
    if len(seen) != len(set(seen)):
        dup = [n for n, c in collections.Counter(seen).items() if c > 1]
        for t in threads:
            if t.questioner in dup:
                t.note = "同じ議員が飛び飛びで現れた。区切りを人が確認すること"
    return threads


# 「令和元年」の「元」は数字ではない。年の欄は数字だけだと令和元年を読めない
# （同じ不具合が bills.py・site.py・kekka.py にもあった。まとめて見直した 2026-09-24）。
_NOTICE_MEETING_RE = re.compile(r"(令和|平成|昭和)\s*(元|[０-９0-9]+)\s*年.*?第\s*([０-９0-9]+)\s*回")
_ERA_BASE = {"昭和": 1925, "平成": 1988, "令和": 2018}
_ZEN = str.maketrans("０１２３４５６７８９", "0123456789")


_FILE_LABEL_RE = re.compile(r"(令和|平成|昭和)\s*(元|[０-９0-9]+)\s*年\s*([０-９0-9]+)\s*月")


def notice_year_month(filename: str) -> str | None:
    """通告書のファイル名「12067_令和8年6月.pdf」→「2026-06」。

    **会議名では突き合わせられない。** 平成期の通告書は
    「一般質問事項（遠賀町議会第４回９月定例会）」のように元号年が入っておらず、
    西暦が決まらない。ファイル名は町HPのリンク文言から作っており、年月が入っている。
    1つの通告書が1つの定例会に対応するので、年月で足りる。
    """
    m = _FILE_LABEL_RE.search(filename.translate(_ZEN))
    if not m:
        return None
    era, yy, mm = m.groups()
    return f"{_ERA_BASE[era] + (1 if yy == '元' else int(yy))}-{int(mm):02d}"


def load_notices(dir_path: Path, aliases: dict[str, str] | None = None) -> dict[str, list]:
    """通告書を読んで、年月 → 通告の一覧にする。

    `aliases` は発言者マスタ（`masters/speakers.json`）のもの。会議録と通告書で
    氏名の表記が違うことがある（濱岡峯達 / 浜岡峯達）ので、同じ別名表をここでも使う。
    """
    from pipeline.parse import tsukokusho as TS

    aliases = aliases or {}
    out: dict[str, list] = collections.defaultdict(list)
    for f in sorted(dir_path.glob("*.pdf")):
        ym = notice_year_month(f.name)
        if not ym:
            continue
        for n in TS.parse(f).notices:
            n.questioner = aliases.get(n.questioner, n.questioner)
            out[ym].append(n)
    return out


def attach_topics(
    threads: list[Thread], notices: dict[str, list], aliases: dict[str, str] | None = None
) -> None:
    """スレッドに通告書の質問事項を結びつける。

    年月と氏名で照合する。通告順は会議全体の通し番号で、一般質問が2日に
    分かれるとスレッドのその日の順番とずれるため、順番では照合しない。

    別名は**会議録側と通告書側の両方**に当てる。片側だけに当てると、
    そろえたはずの氏名どうしが逆に食い違う。
    """
    aliases = aliases or {}
    for t in threads:
        t.questioner = aliases.get(t.questioner, t.questioner)
        cands = [n for n in notices.get(t.on[:7], []) if n.questioner == t.questioner]
        if not cands:
            t.note = (t.note + " / " if t.note else "") + "通告書と対応づかず"
            continue
        t.topics = [{"no": x.no, "title": x.title} for x in cands[0].topics]
        if len(cands) > 1:
            t.note = (t.note + " / " if t.note else "") + "同じ議員の通告が複数あり先頭を採った"


def build(
    paths: list[Path],
    notice_dir: Path | None = None,
    aliases: dict[str, str] | None = None,
) -> list[Thread]:
    out: list[Thread] = []
    for f in sorted(paths):
        tr = T.parse(decode_cp932(f.read_bytes()), f.stem)
        out.extend(split_threads(tr, unid_to_date(f.stem)))
    if notice_dir and notice_dir.exists():
        attach_topics(out, load_notices(notice_dir, aliases), aliases)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一般質問をスレッドに分ける")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[2]
    files = sorted((root / "data" / "raw" / "voices").glob("K_*.html"))
    if not files:
        print("data/raw/voices に会議録がありません。", file=sys.stderr)
        return 1

    from pipeline.parse.speakers import load_overrides

    aliases = load_overrides(root / "masters" / "speakers.json")["aliases"]
    threads = build(files, root / "data" / "raw" / "tsukokusho", aliases)
    days = len({t.unid for t in threads})
    per_day = collections.Counter(collections.Counter(t.unid for t in threads).values())
    flagged = [t for t in threads if t.note]

    print(f"一般質問のスレッド {len(threads)}件 / {days}日")
    print("  1日あたりのスレッド数:", dict(sorted(per_day.items())))
    print(f"  質問者の異なり: {len({t.questioner for t in threads})}人")
    print(f"  1スレッドあたりの発言数: 中央値 "
          f"{sorted(len(t.turns) for t in threads)[len(threads)//2]}")
    with_topics = [t for t in threads if t.topics]
    print(f"  通告書と対応づいた: {len(with_topics)}件"
          f"（{len(with_topics) / len(threads) * 100:.1f}%）")
    if with_topics:
        n = sum(len(t.topics) for t in with_topics)
        print(f"  質問事項の合計: {n}件（1スレッドあたり平均 {n / len(with_topics):.2f}）")
    if flagged:
        print(f"  ⚠ 注記つきのスレッド: {len(flagged)}件")
        for msg, c in collections.Counter(t.note for t in flagged).most_common(5):
            print(f"      {c:>3}件  {msg}")

    if args.report:
        return 0

    out = root / "data" / "normalized" / "threads.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps([asdict(t) for t in threads], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\n→ {out.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
