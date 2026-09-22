"""遠賀町公式ホームページから、会議録を補う資料のPDFを取得する。

2種類だけ取る。どちらも会議録からは復元できない、または照合に使う。

- **一般質問通告書**（平成19年〜）
  `通告順 / 質問者 / 質問事項 / 質問の要旨 / 質問の相手` が表で入っている。
  一般質問の要約を「質問事項ごと」に切るための正解データ。会議録からは復元できない。

- **審議案件・結果**（verify用）
  `議案番号 / 件名 / 議決年月日 / 議決結果`。議決結果は会議録本文からも取れるので
  本文の取得元にはしないが、独立した第二の情報源として突き合わせに使う。

探索はしない。索引ページのリンクだけをたどる（`docs/調査報告_P0.md` の反省）。
町の公式サイトは http だと301を返すので https で取りに行く。

    python -m pipeline.fetch.town --dry-run
    python -m pipeline.fetch.town
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from pipeline.common.http import Fetcher, FetchError
from pipeline.common.state import State

TOWN = "https://www.town.onga.lg.jp"

# 取得の対象は令和元年（2019）から。会議録の取得範囲に合わせる。
# 平成31年以前は今後取得しないと決めた（2026-09-23）。既に取得済みのものは消していない。
FIRST_YEAR = 2019

# 索引ページ。ここから年別ページ → PDF とたどる。
SOURCES = {
    "tsukokusho": {
        "index": f"{TOWN}/site/gikai/list20-62.html",
        "year_link_text": "一般質問通告書",
        "label": "一般質問通告書",
    },
    "kekka": {
        "index": f"{TOWN}/site/gikai/list20-65.html",
        "year_link_text": "審議案件・結果",
        "label": "審議案件・結果",
    },
}

_LINK_RE = re.compile(r"<a\b[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_PDF_SUFFIX_RE = re.compile(r"\s*\[PDF.*?\]\s*$")
_UNSAFE_RE = re.compile(r"[\\/:*?\"<>|【】\s　]+")


def link_text(raw: str) -> str:
    text = html.unescape(_TAG_RE.sub("", raw))
    text = text.replace("​", "").strip()
    return _PDF_SUFFIX_RE.sub("", text)


def safe_name(text: str) -> str:
    return _UNSAFE_RE.sub("", text) or "untitled"


@dataclass(frozen=True)
class Attachment:
    url: str
    attachment_id: str
    label: str
    year_label: str

    @property
    def filename(self) -> str:
        # idで一意性を担保し、ラベルで人が読めるようにする。
        return f"{self.attachment_id}_{safe_name(self.label)}.pdf"


_ZEN_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_ERA_FIRST_YEAR = {"令和": 2018, "平成": 1988, "昭和": 1925}
_ERA_RE = re.compile(r"(令和|平成|昭和)\s*(元|\d+)\s*年")


def label_to_year(label: str) -> int:
    """「一般質問通告書（令和8年）」→ 2026。読めなければ 0。

    ラベルの文字列順で並べると「平成30年」が「令和8年」より先に来てしまうので、
    西暦に直してから並べる。最新年のページを取り直す判定に効く。
    """
    m = _ERA_RE.search(label.translate(_ZEN_DIGITS))
    if not m:
        return 0
    era, num = m.groups()
    return _ERA_FIRST_YEAR[era] + (1 if num == "元" else int(num))


def parse_year_pages(page: str, contains: str) -> list[tuple[str, str]]:
    """索引ページから年別ページの (URL, ラベル) を取り出す。新しい年が先。"""
    found: dict[str, str] = {}
    for href, raw in _LINK_RE.findall(page):
        text = link_text(raw)
        if contains not in text:
            continue
        m = re.match(r"^(/site/gikai/\d+\.html)$", html.unescape(href))
        if not m:
            continue
        found.setdefault(TOWN + m.group(1), text)
    return sorted(found.items(), key=lambda kv: (-label_to_year(kv[1]), kv[1]))


def parse_attachments(page: str, year_label: str) -> list[Attachment]:
    """年別ページから添付PDFを取り出す。"""
    found: dict[str, Attachment] = {}
    for href, raw in _LINK_RE.findall(page):
        m = re.match(r"^(/uploaded/attachment/(\d+)\.pdf)$", html.unescape(href), re.IGNORECASE)
        if not m:
            continue
        attachment_id = m.group(2)
        label = link_text(raw) or f"{year_label}-{attachment_id}"
        found.setdefault(
            attachment_id,
            Attachment(
                url=TOWN + m.group(1),
                attachment_id=attachment_id,
                label=label,
                year_label=year_label,
            ),
        )
    return sorted(found.values(), key=lambda a: a.attachment_id)


def fetch_page(
    fetcher: Fetcher, state: State, root: Path, url: str, path: Path, dry_run: bool = False
) -> str | None:
    """索引・年別ページを取る。手元にあれば取りに行かない。

    `dry_run` のときは一切通信しない。手元になければ None を返す。
    """
    if path.exists():
        return path.read_bytes().decode("utf-8", errors="replace")
    if dry_run:
        return None
    body = fetcher.get(url).body
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    state.record(url, path, body, root)
    return body.decode("utf-8", errors="replace")


def run_source(
    fetcher: Fetcher,
    state: State,
    root: Path,
    key: str,
    dry_run: bool,
    refresh_index: bool,
    from_year: int = FIRST_YEAR,
) -> tuple[int, int]:
    source = SOURCES[key]
    out_dir = root / "data" / "raw" / key
    pages_dir = out_dir / "_pages"

    print(f"■ {source['label']}")

    index_path = pages_dir / "index.html"
    if refresh_index and not dry_run and index_path.exists():
        index_path.unlink()
    index_page = fetch_page(fetcher, state, root, source["index"], index_path, dry_run)
    if index_page is None:
        print("  [取得予定] 索引ページ（未取得のため、この先は数えられません）")
        return 0, 0

    year_pages = [
        (url, label) for url, label in parse_year_pages(index_page, source["year_link_text"])
        if label_to_year(label) == 0 or label_to_year(label) >= from_year
    ]
    print(f"  年別ページ {len(year_pages)}件（{from_year}年以降）")

    attachments: list[Attachment] = []
    for year_url, year_label in year_pages:
        year_path = pages_dir / (year_url.rsplit("/", 1)[-1])
        # 最新年のページは新しいPDFが足されるので取り直す。
        if refresh_index and not dry_run and year_label == year_pages[0][1] and year_path.exists():
            year_path.unlink()
        page = fetch_page(fetcher, state, root, year_url, year_path, dry_run)
        if page is None:
            print(f"  [取得予定] 年別ページ {year_label}")
            continue
        attachments.extend(parse_attachments(page, year_label))

    fetched = skipped = 0
    for a in attachments:
        path = out_dir / a.filename
        if path.exists() and state.has(a.url):
            skipped += 1
            continue
        if dry_run:
            print(f"  [取得予定] {a.year_label} / {a.label}")
            fetched += 1
            continue
        try:
            res = fetcher.get(a.url)
        except FetchError as e:
            print(f"  [失敗] {a.url}: {e}", file=sys.stderr)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(res.body)
        state.record(a.url, path, res.body, root)
        fetched += 1
        print(f"  {a.year_label} / {a.label}  {len(res.body):,} bytes")

    return fetched, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="遠賀町公式HPから通告書・審議案件結果のPDFを取得する")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--only", choices=sorted(SOURCES), help="片方だけ取得する"
    )
    parser.add_argument(
        "--from-year", type=int, default=FIRST_YEAR,
        help=f"この年以降の通告書・結果を取得する（既定 {FIRST_YEAR}）",
    )
    parser.add_argument(
        "--refresh-index",
        action="store_true",
        help="索引ページと最新年のページを取り直す（月次更新で使う）",
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[2]
    state = State(root / "state.json")
    fetcher = Fetcher()

    total_fetched = total_skipped = 0
    for key in sorted(SOURCES):
        if args.only and key != args.only:
            continue
        fetched, skipped = run_source(
            fetcher, state, root, key, args.dry_run, args.refresh_index, args.from_year
        )
        total_fetched += fetched
        total_skipped += skipped

    if not args.dry_run:
        state.save()
        print(f"\nリクエスト {fetcher.request_count}件 / 取得 {total_fetched}件 / スキップ {total_skipped}件")
        print(f"state.json に {len(state)}件を記録しました。")
    else:
        print(f"\n（--dry-run）取得予定 {total_fetched}件 / 取得済み {total_skipped}件")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
