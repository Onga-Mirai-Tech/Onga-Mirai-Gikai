"""要約モデルの比較（P4の最初の一歩）。

数件を2つのモデルに投げ、**人が読んで決めるための材料**を1枚のMarkdownに並べる。
`docs/設計ドラフト.md`「使い方」が言う「まず軽量モデルと中位モデルで数件ずつ試し、
品質と費用を比べて決める」の実施にあたる。

**費用は選定の理由にしない**（設計ドラフト）。ここで費用を出すのは、
初回一括の見積もりが実測とずれていないかを確かめるためのもの。

出力は `data/summaries/_compare/`（private の data リポジトリ側）に置く。
原文の抜粋を含むため、公開リポジトリには入れない。

    .venv/bin/python3 -m pipeline.summarize.compare --dry-run
    .venv/bin/python3 -m pipeline.summarize.compare
"""

from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from pipeline.summarize import context as C
from pipeline.summarize import prompt as P

# 比較するモデル。軽量（Haiku）と中位（Sonnet）。
MODELS = ["claude-haiku-4-5-20251001", "claude-sonnet-5"]

# 100万トークンあたりのドル。設計ドラフト「費用の考え方」と同じ前提（2026-06-24時点）。
# Batch API は50%引きだが、ここでは通常価格のまま出す（比較は同時実行で行うため）。
PRICE = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
}

FIRST_YEAR = "2019"


def load_key(root: Path) -> str:
    """APIキーは環境変数、なければ手元の .env から読む。リポジトリには置かない。"""
    if key := os.environ.get("ANTHROPIC_API_KEY"):
        return key
    env = root / ".env"
    if env.exists():
        m = re.search(r"^\s*ANTHROPIC_API_KEY\s*=\s*(.*)$", env.read_text(encoding="utf-8"), re.M)
        if m and m.group(1).strip():
            return m.group(1).strip().strip("'\"")
    raise SystemExit("ANTHROPIC_API_KEY がありません。.env に入れてください。")


def pick(root: Path) -> list[tuple[str, str, dict]]:
    """比較の対象を選ぶ。長さの偏りが品質に出るので、短い・中くらい・長いを混ぜる。"""
    threads, bills = C.load_normalized(root)

    th = [t for t in threads if t["on"][:4] >= FIRST_YEAR and t["topics"]]
    th.sort(key=lambda t: len(C.thread_input(root, t)))
    tb = [b for b in bills if b["meeting_id"][:4] >= FIRST_YEAR]
    tb.sort(key=lambda b: len(C.bill_input(root, b)))

    return [
        ("thread", "一般質問（短い）", th[len(th) // 10]),
        ("thread", "一般質問（中くらい）", th[len(th) // 2]),
        ("thread", "一般質問（長い）", th[-1]),
        ("bill", "議案（中くらい）", tb[len(tb) // 2 + len(tb) // 4]),
        ("bill", "議案（長い・当初予算）", tb[-1]),
    ]


_DIGITS = re.compile(r"[0-9０-９][0-9０-９,，．.]*")


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).replace(",", "").replace("，", "")


def unmatched_numbers(summary: str, source: str) -> list[str]:
    """要約に出てくる数字が原文にあるか。なければ、作られた数字の疑いがある。

    検証（`pipeline/verify`）の先取り。モデル選定でいちばん重い判断材料になる。
    """
    src = _norm(source)
    out = []
    for m in _DIGITS.finditer(summary):
        n = _norm(m.group(0)).rstrip(".")
        if n and n not in src:
            out.append(m.group(0))
    return sorted(set(out))


def bad_huids(data: dict, source: str) -> list[int]:
    """引用された発言IDが本文に実在するか。"""
    present = {int(x) for x in re.findall(r"^\[(\d+)\]", source, re.M)}
    return sorted({h for h in _all_huids(data) if h not in present})


def _dicts(seq) -> list[dict]:
    """要素が dict でないものは捨てる。

    モデルが `points` を文字列の配列で返すことがある。形が違うだけで
    回収処理全体が止まるのは困るので、ここで吸収して呼び出し側で弾く。
    """
    return [x for x in (seq if isinstance(seq, list) else []) if isinstance(x, dict)]


def _all_huids(data: dict) -> list[int]:
    """一般質問は topics[].points[].huids、議案は points[].huids。"""
    out: list[int] = []
    for t in _dicts(data.get("topics")):
        for pt in _dicts(t.get("points")):
            out += [int(h) for h in pt.get("huids", []) if isinstance(h, (int, str))]
    for pt in _dicts(data.get("points")):
        out += [int(h) for h in pt.get("huids", []) if isinstance(h, (int, str))]
    return out


def run_one(client, kind: str, body: str, model: str) -> tuple[dict, object]:
    params = P.build(kind, body, model)
    msg = client.messages.create(**params)
    data = next((b.input for b in msg.content if b.type == "tool_use"), {})
    return data, msg.usage


def render(kind: str, data: dict) -> str:
    lines = []
    if kind == "thread":
        for t in _dicts(data.get("topics")):
            lines.append(f"**{t.get('no')}. {t.get('title', '')}**")
            lines.append("")
            for pt in _dicts(t.get("points")):
                lines.append(f"- 質問: {pt.get('question', '')}")
                lines.append(f"- 答弁: {pt.get('answer', '')}")
                lines.append(f"- 根拠: {pt.get('huids', [])}")
                lines.append("")
    else:
        for p in _dicts(data.get("points")):
            lines.append(f"**{p.get('kind')}**  根拠: {p.get('huids', [])}")
            lines.append(p.get("text", ""))
            lines.append("")
    return "\n".join(lines).strip() or "（出力なし）"


def flat_text(kind: str, data: dict) -> str:
    if kind == "thread":
        return " ".join(
            " ".join([t.get("title", "")]
                     + [f"{pt.get('question','')} {pt.get('answer','')}"
                        for pt in _dicts(t.get("points"))])
            for t in _dicts(data.get("topics")))
    return " ".join(p.get("text", "") for p in _dicts(data.get("points")))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="要約モデルを数件で比較する")
    ap.add_argument("--dry-run", action="store_true", help="投げずに、対象と入力サイズだけ出す")
    ap.add_argument("--models", nargs="*", default=MODELS)
    args = ap.parse_args(argv)

    root = Path(__file__).resolve().parents[2]
    items = pick(root)

    bodies = [(kind, label, item,
               C.thread_input(root, item) if kind == "thread" else C.bill_input(root, item))
              for kind, label, item in items]

    print(f"■ 比較対象 {len(bodies)}件 × モデル {len(args.models)}件")
    for kind, label, item, body in bodies:
        name = item.get("questioner") or f"{item.get('number')} {item.get('title')}"
        print(f"  {label}: {item['meeting']} / {name} / {len(body):,}字")
    if args.dry_run:
        print("\n（--dry-run）送信していません。")
        return 0

    import anthropic

    client = anthropic.Anthropic(api_key=load_key(root))

    out_dir = root / "data" / "summaries" / "_compare"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")

    md = [f"# 要約モデルの比較  {stamp}", "",
          f"プロンプト版: `{P.PROMPT_VERSION}`", "",
          "原文の抜粋を含む。公開リポジトリに置かないこと。", ""]
    totals: dict[str, list[int]] = {m: [0, 0] for m in args.models}

    for kind, label, item, body in bodies:
        name = item.get("questioner") or f"{item.get('number')} {item.get('title')}"
        md += [f"## {label}", "",
               f"- {item['meeting']}　{name}",
               f"- 入力 {len(body):,}字", ""]
        for model in args.models:
            print(f"  → {label} / {model}")
            data, usage = run_one(client, kind, body, model)
            totals[model][0] += usage.input_tokens
            totals[model][1] += usage.output_tokens

            text = flat_text(kind, data)
            nums = unmatched_numbers(text, body)
            huids = bad_huids(data, body)
            checks = []
            checks.append(f"原文にない数字: {('、'.join(nums)) if nums else 'なし'}")
            checks.append(f"実在しない発言ID: {(huids) if huids else 'なし'}")
            if kind == "thread":
                want = [t["title"] for t in item["topics"]]
                got = [t.get("title", "") for t in data.get("topics", [])]
                checks.append(f"質問事項の数: 通告書 {len(want)} / 要約 {len(got)}"
                              + ("" if len(want) == len(got) else "  ← 不一致"))
                # 文言が変わると通告書との突き合わせができなくなる。数だけでは足りない。
                if want != got:
                    checks.append(f"質問事項の文言: 通告書と違う　{got}")
                n = sum(len(t.get("points", [])) for t in data.get("topics", []))
                checks.append(f"やりとりの要点: {n}件")

            md += [f"### {model}", "",
                   "".join(f"- {c}\n" for c in checks),
                   f"- トークン: 入力 {usage.input_tokens:,} / 出力 {usage.output_tokens:,}", "",
                   render(kind, data), ""]
            (out_dir / f"{stamp}_{item['id']}_{model}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    md += ["## 費用", "",
           "| モデル | 入力 | 出力 | この比較の費用 |", "|---|---|---|---|"]
    for model in args.models:
        i, o = totals[model]
        pi, po = PRICE.get(model, (0, 0))
        md.append(f"| {model} | {i:,} | {o:,} | ${i / 1e6 * pi + o / 1e6 * po:.4f} |")
    md.append("")

    path = out_dir / f"{stamp}_比較.md"
    path.write_text("\n".join(md), encoding="utf-8")
    print(f"\n{path} に書きました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
