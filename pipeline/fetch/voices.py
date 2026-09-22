"""遠賀町議会 会議録検索システム（VOICES）から会議録原文を取得する。

URL規則は `docs/調査報告_P0.md` の「1. VOICES の基本」に実物で確かめたものを使う。

    年一覧: cgi/voiweb.exe?ACT=100&KENSAKU=0&SORT=0&KTYP=0,1,2,3&KGTP={1|2}&YEAR={西暦}
    本文  : cgi/voiweb.exe?ACT=203&FINO={日程ID}&HATSUGENMODE=1&HYOUJIMODE=0&STYLE=0

`ACT=203` は1日分の全文を1レスポンスで返す。ページ送りはない。
Cookie も POST も要らない。素の GET だけで取れる。

    python -m pipeline.fetch.voices --dry-run     何件取りに行くかだけ出す
    python -m pipeline.fetch.voices               未取得分だけ取る
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from pipeline.common.http import Fetcher, FetchError, decode_cp932
from pipeline.common.state import JST, State

BASE = "http://iasb-sv.town.onga.lg.jp/voices/cgi/"
LIST_URL = BASE + "voiweb.exe?ACT=100&KENSAKU=0&SORT=0&KTYP=0,1,2,3&KGTP={kgtp}&YEAR={year}"
BODY_URL = BASE + "voiweb.exe?ACT=203&FINO={fino}&HATSUGENMODE=1&HYOUJIMODE=0&STYLE=0"

# KGTP は会議種別。詳細検索の「会議の種類」がこの2つしか持っていない（委員会はない）。
KGTP_TEIREIKAI = 1
KGTP_RINJIKAI = 2
KGTP_LABEL = {KGTP_TEIREIKAI: "定例会", KGTP_RINJIKAI: "臨時会"}

# VOICES の収録範囲。平成17年（2005）から。
FIRST_YEAR = 2005

# 元号の開始年 - 1。和暦YY年 = 基準 + YY。
ERA_BASE = {"S": 1925, "H": 1988, "R": 2018}

# 一覧の1行。会議名 + ACT=200リンク（そのテキストが「06月04日-01号」）。
_ROW_RE = re.compile(
    r"</A>\s*([^,<>]+?)\s*,\s*<A\s+HREF=\"(voiweb\.exe\?ACT=200[^\"]+)\"[^>]*>([^<]+)</A>",
    re.IGNORECASE,
)
_HIT_RE = re.compile(r"(\d+)\s*件の日程")
_UNID_RE = re.compile(r"^K_([SHR])(\d{2})(\d{2})(\d{2})(\d{4})(\d)$")

_ZEN_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def normalize(text: str) -> str:
    """全角数字を半角にし、空白（全角含む）を落とす。

    会議名は「令和　８年第　３回定例会」のように全角スペースで字送りされている。
    """
    return text.translate(_ZEN_DIGITS).replace("　", "").replace(" ", "").strip()


def unid_to_date(unid: str) -> date:
    """UNID から開催日を取り出す。

    `K_R08060400011` = K_ + 元号R + 年08 + 月06 + 日04 + 号0001 + 1
    """
    m = _UNID_RE.match(unid)
    if not m:
        raise ValueError(f"UNIDの形式が想定と違う: {unid}")
    era, yy, mm, dd, _no, _tail = m.groups()
    return date(ERA_BASE[era] + int(yy), int(mm), int(dd))


_LABEL_RE = re.compile(r"(\d{1,2})月(\d{1,2})日-(\d{1,3})号")


def parse_issue_label(label: str) -> tuple[int, int, int]:
    """リンクのテキスト「06月08日-02号」から (月, 日, 号) を取り出す。

    号は UNID の数値からは取らない。定例会は `0001`、臨時会は `0101` のように
    採番が違い、そのまま整数にすると臨時会が101号になってしまうため。
    """
    m = _LABEL_RE.search(label.translate(_ZEN_DIGITS))
    if not m:
        raise ValueError(f"日付・号の表記が想定と違う: {label!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


@dataclass(frozen=True)
class Schedule:
    """会議録の1日分（VOICES の言う「日程」）。"""

    unid: str
    fino: int
    kgno: int
    kgtp: int
    meeting_name: str  # 「令和8年第3回定例会」（正規化済み）
    held_on: date
    issue_no: int  # 「-02号」の 2

    @property
    def body_url(self) -> str:
        return BODY_URL.format(fino=self.fino)

    @property
    def filename(self) -> str:
        return f"{self.unid}.html"


def parse_hit_count(page: str) -> int | None:
    m = _HIT_RE.search(page)
    return int(m.group(1)) if m else None


def parse_schedule_list(page: str, kgtp: int) -> list[Schedule]:
    """年一覧のHTMLから、その年の日程を取り出す。"""
    schedules: list[Schedule] = []
    seen: set[str] = set()

    for meeting_name, href_raw, label in _ROW_RE.findall(page):
        href = html.unescape(href_raw)
        params = dict(re.findall(r"[?&]([A-Z_]+)=([^&]*)", href))
        unid = params.get("UNID", "")
        if not unid or unid in seen:
            continue
        try:
            held_on = unid_to_date(unid)
            month, day, issue_no = parse_issue_label(html.unescape(label))
        except ValueError:
            # 形式が想定外のものは拾わない。取りこぼしは件数の突き合わせで気づける。
            continue
        if (held_on.month, held_on.day) != (month, day):
            # UNIDの日付と表示の日付が食い違うのは、こちらの読み違いを意味する。
            raise ValueError(f"UNIDと表示の日付が一致しない: {unid} / {label!r}")
        seen.add(unid)
        schedules.append(
            Schedule(
                unid=unid,
                fino=int(params["FINO"]),
                kgno=int(params["KGNO"]),
                kgtp=kgtp,
                meeting_name=normalize(meeting_name),
                held_on=held_on,
                issue_no=issue_no,
            )
        )

    return schedules


def is_night_jst(now: datetime | None = None) -> bool:
    """日本時間の深夜帯（0時〜6時）か。

    CLAUDE.md の「実行は日本時間の深夜帯」を確認するためのもの。
    """
    now = now or datetime.now(JST)
    return 0 <= now.astimezone(JST).hour < 6


def collect_schedules(
    fetcher: Fetcher,
    state: State,
    root: Path,
    years: list[int],
    refresh_years: set[int],
    dry_run: bool,
) -> list[Schedule]:
    """年一覧をたどって、全日程の一覧を作る。

    一覧HTMLが手元にあり、その年が更新対象でなければ取りに行かない。
    会議録は確定したら変わらないため。
    """
    list_dir = root / "data" / "raw" / "voices" / "_list"
    all_schedules: list[Schedule] = []

    for kgtp in (KGTP_TEIREIKAI, KGTP_RINJIKAI):
        for year in years:
            url = LIST_URL.format(kgtp=kgtp, year=year)
            path = list_dir / f"kgtp{kgtp}-{year}.html"
            must_refresh = year in refresh_years

            if path.exists() and not must_refresh:
                page = decode_cp932(path.read_bytes())
            elif dry_run:
                print(f"  [取得予定] 一覧 {KGTP_LABEL[kgtp]} {year}年")
                continue
            else:
                body = fetcher.get(url).body
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                state.record(url, path, body, root)
                page = decode_cp932(body)

            found = parse_schedule_list(page, kgtp)
            all_schedules.extend(found)

    return all_schedules


def fetch_bodies(
    fetcher: Fetcher,
    state: State,
    root: Path,
    schedules: list[Schedule],
    dry_run: bool,
    limit: int | None,
) -> tuple[int, int]:
    """本文を取る。取得済みはスキップする。"""
    out_dir = root / "data" / "raw" / "voices"
    fetched = skipped = 0

    for s in schedules:
        path = out_dir / s.filename
        if path.exists() and state.has(s.body_url):
            skipped += 1
            continue

        if dry_run:
            print(f"  [取得予定] {s.held_on} {s.meeting_name} {s.issue_no:02d}号  FINO={s.fino}")
            fetched += 1
            if limit and fetched >= limit:
                break
            continue

        try:
            res = fetcher.get(s.body_url)
        except FetchError as e:
            print(f"  [失敗] {s.unid}: {e}", file=sys.stderr)
            continue

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(res.body)
        state.record(s.body_url, path, res.body, root)
        fetched += 1
        print(f"  {s.held_on} {s.meeting_name} {s.issue_no:02d}号  {len(res.body):,} bytes")

        if limit and fetched >= limit:
            break

    return fetched, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VOICES から会議録原文を取得する")
    parser.add_argument("--dry-run", action="store_true", help="取得せず、対象だけ表示する")
    parser.add_argument("--limit", type=int, help="この件数だけ取得する（動作確認用）")
    parser.add_argument("--from-year", type=int, default=FIRST_YEAR)
    parser.add_argument("--to-year", type=int, default=date.today().year)
    parser.add_argument(
        "--allow-daytime",
        action="store_true",
        help="深夜帯以外でも実行する。少数の動作確認以外では使わないこと",
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[2]
    state = State(root / "state.json")
    years = list(range(args.from_year, args.to_year + 1))
    # 直近2年だけ一覧を取り直す。新しい会議が増えるのはこの範囲だけ。
    refresh_years = {args.to_year, args.to_year - 1}

    if not args.dry_run and not is_night_jst() and not args.allow_daytime:
        print(
            "いまは日本時間の深夜帯（0〜6時）ではありません。\n"
            "CLAUDE.md の取得マナーに従い、一括取得は深夜帯に実行してください。\n"
            "動作確認なら --limit を付けたうえで --allow-daytime を付けてください。",
            file=sys.stderr,
        )
        return 1

    fetcher = Fetcher()

    print(f"■ 年一覧（{years[0]}〜{years[-1]}年、定例会・臨時会）")
    schedules = collect_schedules(fetcher, state, root, years, refresh_years, args.dry_run)
    schedules.sort(key=lambda s: (s.held_on, s.issue_no))
    teirei = sum(1 for s in schedules if s.kgtp == KGTP_TEIREIKAI)
    print(f"  日程 {len(schedules)}件（定例会 {teirei} / 臨時会 {len(schedules) - teirei}）")

    print("■ 本文")
    fetched, skipped = fetch_bodies(fetcher, state, root, schedules, args.dry_run, args.limit)

    if not args.dry_run:
        state.save()
        print(f"\nリクエスト {fetcher.request_count}件 / 取得 {fetched}件 / スキップ {skipped}件")
        print(f"state.json に {len(state)}件を記録しました。")
    else:
        print(f"\n（--dry-run）取得予定 {fetched}件 / 取得済み {skipped}件")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
