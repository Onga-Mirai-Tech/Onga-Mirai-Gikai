"""会議録から取り出した議決結果を、町公式HPの「審議案件・結果」PDFと突き合わせる。

`docs/設計ドラフト.md`「検証（verify）」の実施。**独立した第二の情報源**による照合なので、
会議録の読み取りの取りこぼしを拾える。

サイトに載せる議決結果は会議録本文から取ったものを使う。PDFは照合にだけ使う
（PDFは会議録より簡略で、質疑・討論の経過を持たないため）。

    python -m pipeline.verify.results            不一致を一覧で出す
    python -m pipeline.verify.results --report data/verify/結果照合.md
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from pipeline.parse import kekka as K

FIRST_YEAR = "2019"

# PDFと会議録で言い方が違うもの。意味が同じものだけを揃える。
# 「修正可決」は「可決」と区別する（修正されたかどうかは事実として違う）。
_SAME = {"原案可決": "可決", "原案認定": "認定", "原案承認": "承認"}


def normalize_result(word: str) -> str:
    return _SAME.get(word, word)


@dataclass
class Mismatch:
    meeting_id: str
    number: str
    title: str
    kind: str  # 不一致の種類
    from_minutes: str  # 会議録から取った値
    from_pdf: str  # PDFの値


def load_pdfs(root: Path, first_year: str = FIRST_YEAR) -> tuple[dict[str, dict[str, K.Item]], list[str]]:
    """PDFを読み、会議IDごとに 議案番号→Item の表を作る。"""
    out: dict[str, dict[str, K.Item]] = defaultdict(dict)
    unreadable: list[str] = []
    for f in sorted((root / "data" / "raw" / "kekka").glob("*.pdf")):
        try:
            pages = K.extract_pages(f)
        except Exception as e:  # PDFの作りが違うものがある。止めずに記録して進む。
            unreadable.append(f"{f.name}: {type(e).__name__}")
            continue

        # **ページごとに会議を判定する。** 1つのPDFに別の会議が綴じ込まれて
        # いることがある（実測: 平成29年の臨時会が令和3年のPDFに入っていた）。
        # 表のないページ（続き）は、直前のページの会議に属する。
        mid, base = "", None
        seen = False
        for page in pages:
            found = K.meeting_id(page)
            if found:
                mid, base = found, K.era_base(page)
            if not mid:
                continue
            seen = True
            if mid[:4] < first_year:
                continue
            # 表題のないページ（表の続き）には元号がない。前のページから引き継ぐ。
            for item in K.parse(page, base):
                out[mid].setdefault(item.number, item)
        if not seen:
            unreadable.append(f"{f.name}: 会議名が読めない")
    return dict(out), unreadable


def compare(bills: list[dict], pdfs: dict[str, dict[str, K.Item]]) -> tuple[list[Mismatch], list[str]]:
    """会議録側とPDF側を突き合わせる。**PDFにある会議だけ**を対象にする。

    PDFは会議録より早く公開される。会議録がまだ1件もない会議は
    「不一致」ではなく「会議録が未公開」として分けて数える。
    """
    out: list[Mismatch] = []
    pending: list[str] = []
    by_meeting: dict[str, dict[str, dict]] = defaultdict(dict)
    for b in bills:
        by_meeting[b["meeting_id"]][b["number"]] = b

    for mid, items in sorted(pdfs.items()):
        minutes = by_meeting.get(mid, {})
        if not minutes:
            pending.append(f"{mid}（PDFに{len(items)}件。会議録がまだ公開されていません）")
            continue
        for number, item in sorted(items.items()):
            b = minutes.get(number)
            if b is None:
                out.append(Mismatch(mid, number, item.title, "会議録に見当たらない", "", item.result))
                continue
            got, want = b.get("result") or "", normalize_result(item.result)
            if want and normalize_result(got) != want:
                out.append(Mismatch(mid, number, item.title, "議決結果が違う", got or "（取れていない）", item.result))
            if item.decided_on and b.get("decided_on") and item.decided_on != b["decided_on"]:
                out.append(Mismatch(mid, number, item.title, "議決年月日が違う", b["decided_on"], item.decided_on))

        for number, b in sorted(minutes.items()):
            # 「報告」は議決する案件ではないので、審議案件・結果のPDFには載らない。
            # 載っていないのが正しい。
            if number.startswith("報告"):
                continue
            if number not in items:
                out.append(Mismatch(mid, number, b["title"], "PDFに見当たらない", b.get("result") or "", ""))
    return out, pending


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="議決結果を町公式HPのPDFと突き合わせる")
    ap.add_argument("--report", type=Path, help="結果をMarkdownで書き出す")
    ap.add_argument("--from-year", default=FIRST_YEAR)
    args = ap.parse_args(argv)

    root = Path(__file__).resolve().parents[2]
    bills = json.loads((root / "data" / "normalized" / "bills.json").read_text(encoding="utf-8"))
    pdfs, unreadable = load_pdfs(root, args.from_year)

    checked = sum(len(v) for v in pdfs.values())
    bad, pending = compare(bills, pdfs)

    print(f"■ PDF {len(pdfs)}会議 / {checked}件を照合")
    print(f"  不一致 {len(bad)}件")
    kinds: dict[str, int] = defaultdict(int)
    for m in bad:
        kinds[m.kind] += 1
    for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
        print(f"    {k}: {n}件")
    if pending:
        print(f"  会議録がまだ公開されていない会議 {len(pending)}件")
        for m in pending:
            print(f"    {m}")
    if unreadable:
        print(f"  読めなかったPDF {len(unreadable)}件（{args.from_year}年より前のものを含む）")

    for m in bad[:40]:
        print(f"  {m.meeting_id} {m.number} [{m.kind}] 会議録={m.from_minutes} / PDF={m.from_pdf}")
    if len(bad) > 40:
        print(f"  …ほか {len(bad) - 40}件")

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# 議決結果の照合  （{checked}件）", "",
                 "会議録から取り出した議決結果を、町公式HPの「審議案件・結果」PDFと突き合わせた結果。",
                 "", f"- 照合できた議案: {checked}件", f"- 不一致: {len(bad)}件", ""]
        if bad:
            lines += ["| 会議 | 議案 | 種類 | 会議録 | PDF | 件名 |", "|---|---|---|---|---|---|"]
            lines += [f"| {m.meeting_id} | {m.number} | {m.kind} | {m.from_minutes} | {m.from_pdf} | {m.title[:40]} |"
                      for m in bad]
        else:
            lines.append("**不一致はありません。**")
        if pending:
            lines += ["", "## 会議録がまだ公開されていない会議", "",
                      "PDFのほうが先に公開される。会議録が出れば自動的に照合される。", ""]
            lines += [f"- {m}" for m in pending]
        if unreadable:
            lines += ["", "## 読めなかったPDF", ""] + [f"- {u}" for u in unreadable]
        args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n{args.report} に書きました。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
