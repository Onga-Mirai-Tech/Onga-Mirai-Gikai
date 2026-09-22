"""発言者マスタを作り、判定できなかったものを一覧に出す。

同じ人が時代で役職を変える（議員 → 議長 → 町長）ので、**氏名を同一性の鍵**にし、
役職は履歴として持つ。役職ラベルそのものは人を指さない。

照合は推測でやらない。**その日の会議録のヘッダ**（出欠表・説明員・書記・議長）と
突き合わせ、どれにも当たらなかったものは `未照合` として一覧に出す。
表記ゆれらしきものは似た氏名を**候補として添えるだけ**で、自動では統合しない。
統合するかどうかは人が決めて `masters/speakers.json` に書く。

    python -m pipeline.parse.speakers            マスタと未判定一覧を書き出す
    python -m pipeline.parse.speakers --report   未判定一覧だけ表示する
"""

from __future__ import annotations

import argparse
import collections
import difflib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pipeline.common.http import decode_cp932
from pipeline.fetch.voices import unid_to_date
from pipeline.parse import transcript as T

# 照合の結果
MATCH_SEAT = "議席一致"  # 議員。出欠表の議席番号と氏名が一致した
MATCH_ROSTER = "名簿一致"  # その日の出欠表・説明員・書記・議長のいずれかに氏名がある
MATCH_ROSTER_OTHER = "別日の名簿一致"  # 同じ氏名・同じ職が、別の日の名簿にある
MATCH_EXTERNAL = "名簿外"  # 名簿に載らない立場（監査委員・参考人・受章者など）
MATCH_NONE = "未照合"  # どれにも当たらない。人の判断が要る

_SEAT_RE = re.compile(r"^([０-９0-9]+)番議員$")
_EXEC_TAIL = ("課長", "室長", "局長", "係長", "次長", "部長", "所長", "管理者", "主査", "主事", "補佐")
_TOP_TITLES = {"町長", "副町長", "助役", "収入役", "教育長"}
_CHAIR_TITLES = {"議長", "副議長", "臨時議長"}


@dataclass
class Observation:
    name: str
    name_raw: str
    title: str
    unid: str
    on: date
    seq: int
    match: str


@dataclass
class Role:
    title: str
    count: int
    first: str
    last: str


@dataclass
class Speaker:
    id: str
    name: str
    kind: str
    roles: list[Role] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    seats: list[str] = field(default_factory=list)
    first: str = ""
    last: str = ""
    count: int = 0
    note: str = ""


def load_overrides(path: Path) -> dict:
    """手で直したマスタを読む。無ければ空。

    aliases  … 会議録の表記 → 正とする氏名（表記ゆれの統合）
    external … 名簿に載らない発言者として確定させる氏名
    slugs    … URLに使うローマ字
    notes    … 人が書き残すメモ
    """
    base = {"aliases": {}, "external": [], "slugs": {}, "notes": {}}
    if path.exists():
        base.update(json.loads(path.read_text(encoding="utf-8")))
    return base


def classify(roles: list[Role]) -> str:
    """その人の区分を決める。**いちばん新しい役職**で決める。

    議員から町長になる人がいる（古野修：3番議員 → 11番議員 → 議長 → 町長）。
    「議席を持ったことがある＝議員」としてしまうと、現職の町長が議員に分類される。

    議長・副議長は議員が務めるので、議席を持ったことがあれば議員とする。
    """
    if not roles:
        return "その他"
    latest = max(roles, key=lambda r: r.last).title
    ever_seat = any(_SEAT_RE.match(r.title) for r in roles)

    if latest in _TOP_TITLES:
        return latest
    if _SEAT_RE.match(latest) or (latest in _CHAIR_TITLES and ever_seat):
        return "議員"
    if latest.endswith(_EXEC_TAIL):
        return "執行部"
    if latest in _CHAIR_TITLES:
        return "議長"
    return "議員" if ever_seat else "その他"


def build_roster(paths: list[Path]) -> dict[str, set[str]]:
    """全期間の名簿を作る（氏名 → その人に付いていた職の集合）。

    臨時会では説明員欄にその職の記載がないことがある。当日のヘッダだけで照合すると
    実在する課長が「未照合」に落ちるので、全期間の名簿も見る。
    """
    roster: dict[str, set[str]] = collections.defaultdict(set)
    for f in paths:
        tr = T.parse(decode_cp932(f.read_bytes()), f.stem)
        for p in tr.executives + tr.clerks:
            roster[p.name].add(p.title)
        for a in tr.attendance:
            roster[a.name].add("議員")
        if tr.chair:
            roster[tr.chair.name].add("議長")
    return roster


def observe(
    tr: T.Transcript,
    on: date,
    aliases: dict[str, str],
    external: set[str],
    roster_all: dict[str, set[str]] | None = None,
) -> list[Observation]:
    """1日分の発言を、その日のヘッダと突き合わせる。"""
    roster_all = roster_all or {}
    seats = {a.seat.strip("　 "): a.name for a in tr.attendance}
    members = {a.name for a in tr.attendance}
    exec_by_title = {e.title: e.name for e in tr.executives}
    roster = members | set(exec_by_title.values()) | {c.name for c in tr.clerks}
    if tr.chair:
        roster.add(tr.chair.name)

    out: list[Observation] = []
    for u in tr.utterances:
        if not u.name:
            continue
        name = aliases.get(u.name, u.name)
        m = _SEAT_RE.match(u.title)
        if m and seats.get(m.group(1) + "番") == name:
            match = MATCH_SEAT
        elif name in roster or aliases.get(u.name) in roster:
            match = MATCH_ROSTER
        elif u.title and u.title in roster_all.get(name, ()):
            # その日の欄には載っていないが、同じ氏名・同じ職が別の日の名簿にある
            match = MATCH_ROSTER_OTHER
        elif name in external or not u.title or not u.title.endswith(_EXEC_TAIL) and not m:
            # 名簿に載らない立場（代表監査委員・人権擁護委員・参考人・受章者など）。
            # 議席ラベルでも執行部の職でもないものは、そもそも名簿に載らない。
            match = MATCH_EXTERNAL if (name in external or not m) else MATCH_NONE
        else:
            match = MATCH_NONE
        out.append(Observation(name, u.name_raw, u.title, tr.unid, on, u.seq, match))
    return out


def build(observations: list[Observation], overrides: dict) -> tuple[list[Speaker], list[dict]]:
    by_name: dict[str, list[Observation]] = collections.defaultdict(list)
    for o in observations:
        by_name[o.name].append(o)

    roster_names = {
        o.name for o in observations
        if o.match in (MATCH_SEAT, MATCH_ROSTER, MATCH_ROSTER_OTHER)
    }
    slugs, notes = overrides["slugs"], overrides["notes"]

    speakers: list[Speaker] = []
    for name, obs in sorted(by_name.items(), key=lambda kv: -len(kv[1])):
        titles = collections.Counter(o.title for o in obs if o.title)
        roles = []
        for title, count in titles.most_common():
            days = sorted(o.on for o in obs if o.title == title)
            roles.append(Role(title=title, count=count, first=str(days[0]), last=str(days[-1])))
        days = sorted(o.on for o in obs)
        seats = sorted({_SEAT_RE.match(t).group(1) + "番" for t in titles if _SEAT_RE.match(t)},
                       key=lambda s: int(re.sub(r"\D", "", s)))
        speakers.append(Speaker(
            id="sp-" + slugs.get(name, name),
            name=name,
            kind=classify(roles),
            roles=roles,
            aliases=sorted({o.name_raw for o in obs if T.squeeze(o.name_raw) != name}),
            seats=seats,
            first=str(days[0]), last=str(days[-1]), count=len(obs),
            note=notes.get(name, ""),
        ))

    # 未照合を氏名ごとにまとめ、似た氏名を候補として添える
    unresolved: list[dict] = []
    unmatched = collections.defaultdict(list)
    for o in observations:
        if o.match == MATCH_NONE:
            unmatched[o.name].append(o)
    for name, obs in sorted(unmatched.items(), key=lambda kv: -len(kv[1])):
        candidates = [
            {"name": c, "似度": round(difflib.SequenceMatcher(None, name, c).ratio(), 2)}
            for c in difflib.get_close_matches(name, sorted(roster_names - {name}), n=3, cutoff=0.6)
        ]
        unresolved.append({
            "name": name,
            "count": len(obs),
            "titles": sorted({o.title for o in obs}),
            "days": sorted({str(o.on) for o in obs})[:5],
            "候補": candidates,
        })
    return speakers, unresolved


def render_report(speakers: list[Speaker], unresolved: list[dict], total: int) -> str:
    lines = ["# 発言者マスタ 未判定一覧", ""]
    matched = total - sum(u["count"] for u in unresolved)
    lines.append(f"発言 {total:,}件中 {matched:,}件（{matched/total*100:.2f}%）はその日の会議録のヘッダと照合できた。")
    lines.append(f"発言者は {len(speakers)}人。")
    lines.append("")
    if not unresolved:
        lines.append("未判定はない。")
        return "\n".join(lines) + "\n"

    lines += [
        f"以下の **{len(unresolved)}件** は、出欠表・説明員・書記・議長のどれにも当たらなかった。",
        "**自動では判断しない。** 直すときは `masters/speakers.json` に書く。",
        "",
        "- 表記ゆれなら `aliases` に `\"会議録の表記\": \"正とする氏名\"` を足す",
        "- 名簿に載らない立場（参考人・受章者・監査委員など）なら `external` に氏名を足す",
        "",
        "| 氏名 | 件数 | 役職ラベル | 初出 | 似た氏名の候補 |",
        "|---|---|---|---|---|",
    ]
    for u in unresolved:
        cand = " / ".join(f"{c['name']}({c['似度']})" for c in u["候補"]) or "—"
        lines.append(
            f"| {u['name']} | {u['count']} | {'、'.join(u['titles']) or '（職の記載なし）'} "
            f"| {u['days'][0]} | {cand} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="発言者マスタを作る")
    parser.add_argument("--report", action="store_true", help="書き出さず、未判定一覧だけ表示する")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[2]
    overrides = load_overrides(root / "masters" / "speakers.json")
    external = set(overrides["external"])
    aliases = overrides["aliases"]

    observations: list[Observation] = []
    files = sorted((root / "data" / "raw" / "voices").glob("K_*.html"))
    if not files:
        print("data/raw/voices に会議録がありません。先に pipeline.fetch.voices を実行してください。",
              file=sys.stderr)
        return 1

    roster_all = build_roster(files)
    for f in files:
        tr = T.parse(decode_cp932(f.read_bytes()), f.stem)
        observations.extend(observe(tr, unid_to_date(f.stem), aliases, external, roster_all))

    speakers, unresolved = build(observations, overrides)
    report = render_report(speakers, unresolved, len(observations))

    if args.report:
        print(report)
        return 0

    out_dir = root / "data" / "normalized"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "speakers.json").write_text(
        json.dumps([_as_dict(s) for s in speakers], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "speakers_unresolved.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"→ data/normalized/speakers.json（{len(speakers)}人）")
    print("→ data/normalized/speakers_unresolved.md")
    return 0


def _as_dict(s: Speaker) -> dict:
    d = {
        "id": s.id, "name": s.name, "kind": s.kind,
        "seats": s.seats, "first": s.first, "last": s.last, "count": s.count,
        "roles": [{"title": r.title, "count": r.count, "first": r.first, "last": r.last} for r in s.roles],
        "aliases": s.aliases,
    }
    if s.note:
        d["note"] = s.note
    return d


if __name__ == "__main__":
    raise SystemExit(main())
