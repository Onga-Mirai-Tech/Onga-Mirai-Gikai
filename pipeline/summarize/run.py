"""AI要約の生成（Batch API）。

    python -m pipeline.summarize.run --dry-run    投げる件数と見積もりだけ出す
    python -m pipeline.summarize.run              未生成のぶんを投げて、完了まで待つ
    python -m pipeline.summarize.run --no-wait    投げるだけ（あとで --collect）
    python -m pipeline.summarize.run --collect    完了したバッチを回収する

**生成済みの要約は作り直さない**（CLAUDE.md）。作り直すときは `--regenerate` を明示する。
プロンプトを変えたら `prompt.PROMPT_VERSION` を上げること。要約JSONに記録しており、
あとからどの版で作ったものかを追える。

要約の単位は `docs/設計ドラフト.md`「7. 要約の単位」に従う。
- 一般質問は議員1人分を1件として投げ、質問事項ごとに分けて受け取る。
- 議案は**質疑または討論に実体があるものだけ**。提案理由と採決しかない議案は投げない。
- 会議・定例会のまとめはAIを使わない（要約の要約を作らないため）。
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from pipeline.summarize import compare as V  # 突き合わせ（unmatched_numbers / bad_huids）
from pipeline.summarize import context as C
from pipeline.summarize import prompt as P

MODEL = "claude-sonnet-5"  # 2026-09-23 決定。docs/設計ドラフト.md「10. 未決事項」
FIRST_YEAR = "2019"

# Batch API の1回あたりの上限に余裕を持たせる。分割しても費用は変わらない。
CHUNK = 500
POLL_SECONDS = 60


def out_dir(root: Path, kind: str) -> Path:
    return root / "data" / "summaries" / ("threads" if kind == "thread" else "bills")


def batch_dir(root: Path) -> Path:
    return root / "data" / "summaries" / "_batches"


def custom_id(kind: str, index: int) -> str:
    """Batch API の custom_id は英数字と `_-` だけ。議案IDは日本語を含むので使えない。

    連番にして、対応表をバッチごとのJSONに残す。途中で落ちても回収できる。
    """
    return f"{kind[0]}{index:05d}"


def targets(root: Path, regenerate: bool) -> list[tuple[str, dict, str]]:
    """要約を作る対象を集める。(種別, 元データ, 入力テキスト) を返す。"""
    threads, bills = C.load_normalized(root)
    items: list[tuple[str, dict, str]] = []

    for t in threads:
        if t["on"][:4] < FIRST_YEAR:
            continue
        if not regenerate and (out_dir(root, "thread") / f"{t['id']}.json").exists():
            continue
        items.append(("thread", t, C.thread_input(root, t)))

    for b in bills:
        if b["meeting_id"][:4] < FIRST_YEAR:
            continue
        # 提案理由と採決しかない議案は、要約するものがない。
        # 会議ページには「質疑・討論はありませんでした」と事実だけ書く。
        if not any(b.get("content", {}).get(k) for k in ("質疑", "討論")):
            continue
        if not regenerate and (out_dir(root, "bill") / f"{b['id']}.json").exists():
            continue
        items.append(("bill", b, C.bill_input(root, b)))

    return items


def _as_list(value):
    """配列であるべきところが文字列で返ってきたときに、中の配列を取り出す。

    実測（令和2年第9回の一般質問ほか）:
      "topics": "\n<parameter name=\"topics\">[ {...}, {...} ]"
    中身は正しいJSONなのに、ツール呼び出しの包み方だけが壊れている。
    捨てて投げ直すと同じ内容にもう一度課金することになるので、拾い直す。

    最初の `[` から最後の `]` までを読む。読めなければ None を返し、
    呼び出し側（shape_ok）が弾く。壊れたものを通さないことが優先。
    """
    if isinstance(value, list):
        # 要素が JSON 文字列で返ることもある（実測: 議案の points）。
        out = []
        for x in value:
            if isinstance(x, str):
                try:
                    x = json.loads(x)
                except ValueError:
                    return None
            if not isinstance(x, dict):
                return None
            out.append(x)
        return out
    if not isinstance(value, str):
        return None
    i, j = value.find("["), value.rfind("]")
    if i < 0 or j <= i:
        return None
    try:
        parsed = json.loads(value[i:j + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, list) else None


def repair(kind: str, data: dict) -> dict:
    """形が崩れた出力を、**内容を変えずに**組み直す。読めなければそのまま返す。"""
    if not isinstance(data, dict):
        return data
    key = "topics" if kind == "thread" else "points"
    top = _as_list(data.get(key))
    if top is None:
        return data
    if kind == "thread":
        for topic in top:
            if (pts := _as_list(topic.get("points"))) is not None:
                topic["points"] = pts
    return {**data, key: top}


def shape_ok(kind: str, data: dict) -> bool:
    """要約が想定した形になっているか。

    ツールのJSON Schemaを指定していても、そのとおりに返ってこないことがある
    （実測: 議案の `points` が文字列の配列で返った）。形が違うものは**保存しない**。
    保存すると「生成済み」とみなされ、欠けたまま二度と作り直されなくなる。
    """
    if not isinstance(data, dict):
        return False
    if kind == "thread":
        topics = data.get("topics")
        if not isinstance(topics, list) or not topics:
            return False
        return all(
            isinstance(t, dict) and isinstance(t.get("points"), list) and t["points"]
            and all(isinstance(pt, dict) and pt.get("question") and pt.get("answer")
                    for pt in t["points"])
            for t in topics
        )
    points = data.get("points")
    if not isinstance(points, list) or not points:
        return False
    return all(isinstance(p, dict) and p.get("kind") and p.get("text") for p in points)


def verify(kind: str, data: dict, body: str) -> dict:
    """機械で見られるところだけ見る。ここで落ちたものは人が確認する。

    `docs/設計ドラフト.md`「検証（verify）」のうち、要約単体で判定できるもの。
    議決結果とPDFの突き合わせは `pipeline/verify` 側で別に行う。
    """
    text = V.flat_text(kind, data)
    issues = []
    if nums := V.unmatched_numbers(text, body):
        issues.append({"kind": "原文にない数字", "detail": nums})
    if huids := V.bad_huids(data, body):
        issues.append({"kind": "実在しない発言ID", "detail": huids})
    if not V._all_huids(data):
        issues.append({"kind": "根拠の発言IDがない", "detail": []})
    return {"verified": not issues, "issues": issues}


def save(root: Path, kind: str, item: dict, data: dict, body: str, usage: dict) -> bool:
    d = out_dir(root, kind)
    d.mkdir(parents=True, exist_ok=True)
    checks = verify(kind, data, body)
    payload = {
        "id": item["id"],
        "kind": kind,
        "model": MODEL,
        "prompt_version": P.PROMPT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "usage": usage,
        **checks,
        "summary": data,
    }
    (d / f"{item['id']}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return checks["verified"]


def submit(client, root: Path, items: list, start: int) -> str:
    requests = []
    mapping = {}
    for i, (kind, item, body) in enumerate(items, start=start):
        cid = custom_id(kind, i)
        mapping[cid] = {"id": item["id"], "kind": kind}
        requests.append({"custom_id": cid, "params": P.build(kind, body, MODEL)})

    batch = client.messages.batches.create(requests=requests)
    batch_dir(root).mkdir(parents=True, exist_ok=True)
    (batch_dir(root) / f"{batch.id}.json").write_text(
        json.dumps({"batch_id": batch.id, "model": MODEL,
                    "prompt_version": P.PROMPT_VERSION,
                    "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "mapping": mapping}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"  バッチ {batch.id} を作成（{len(requests)}件）")
    return batch.id


def wait(client, batch_id: str) -> None:
    while True:
        b = client.messages.batches.retrieve(batch_id)
        c = b.request_counts
        print(f"  {b.processing_status}  成功{c.succeeded} / 失敗{c.errored} "
              f"/ 処理中{c.processing}", flush=True)
        if b.processing_status == "ended":
            return
        time.sleep(POLL_SECONDS)


def collect(client, root: Path, batch_id: str) -> tuple[int, int, int]:
    """結果を回収して保存する。入力テキストは検証のため組み立て直す。"""
    meta = json.loads((batch_dir(root) / f"{batch_id}.json").read_text(encoding="utf-8"))
    mapping = meta["mapping"]

    threads, bills = C.load_normalized(root)
    by_id = {t["id"]: ("thread", t) for t in threads}
    by_id.update({b["id"]: ("bill", b) for b in bills})

    # 終わっていないバッチには結果がない。SDKはここで例外を投げるので、
    # 手前で状態を見て、何が起きているかが分かる形で止める。
    b = client.messages.batches.retrieve(batch_id)
    if b.processing_status != "ended":
        c = b.request_counts
        print(f"  まだ処理中です（成功{c.succeeded} / 失敗{c.errored} / 処理中{c.processing}）。"
              "\n  終わってから、同じコマンドをもう一度実行してください。")
        return 0, 0, 0

    saved = flagged = failed = 0
    for res in client.messages.batches.results(batch_id):
        m = mapping.get(res.custom_id)
        if m is None:
            print(f"  [不明なID] {res.custom_id}")
            continue
        if res.result.type != "succeeded":
            print(f"  [失敗] {m['id']}: {res.result.type}")
            failed += 1
            continue

        msg = res.result.message
        if msg.stop_reason == "max_tokens":
            # 途中で切れた出力は**保存しない**。保存すると「生成済み」とみなされ、
            # 欠けたまま二度と作り直されない。ファイルを作らなければ次回また投げる。
            print(f"  [出力が上限で切れた] {m['id']}")
            failed += 1
            continue
        data = next((b.input for b in msg.content if b.type == "tool_use"), None)
        if data is None:
            print(f"  [出力なし] {m['id']}")
            failed += 1
            continue

        kind, item = by_id[m["id"]]
        data = repair(kind, data)
        if not shape_ok(kind, data):
            print(f"  [形が違う] {m['id']}")
            failed += 1
            continue
        body = C.thread_input(root, item) if kind == "thread" else C.bill_input(root, item)
        ok = save(root, kind, item, data, body,
                  {"input_tokens": msg.usage.input_tokens,
                   "output_tokens": msg.usage.output_tokens})
        saved += 1
        if not ok:
            flagged += 1
    return saved, flagged, failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AI要約を Batch API で生成する")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-wait", action="store_true", help="投げるだけで待たない")
    ap.add_argument("--collect", metavar="BATCH_ID", nargs="?", const="all",
                    help="完了したバッチを回収する（省略時は未回収のものすべて）")
    ap.add_argument("--regenerate", action="store_true",
                    help="生成済みも作り直す。プロンプトを変えたときだけ使う")
    ap.add_argument("--limit", type=int, help="この件数だけ投げる（動作確認用）")
    ap.add_argument("--reverify", action="store_true",
                    help="保存済みの要約を、いまの検証でもう一度確かめる（APIは使わない）")
    ap.add_argument("--only", choices=("thread", "bill"),
                    help="一般質問だけ / 議案だけ。残高に余裕がないときに分けて回す")
    args = ap.parse_args(argv)

    root = Path(__file__).resolve().parents[2]

    if args.reverify:
        # 検証のやり方を直したときに使う。要約そのものは作り直さない。
        threads, bills = C.load_normalized(root)
        by_id = {t["id"]: ("thread", t) for t in threads}
        by_id.update({b["id"]: ("bill", b) for b in bills})
        changed = still = 0
        for sub in ("threads", "bills"):
            for f in sorted((root / "data" / "summaries" / sub).glob("*.json")):
                s = json.loads(f.read_text(encoding="utf-8"))
                kind, item = by_id[s["id"]]
                body = (C.thread_input(root, item) if kind == "thread"
                        else C.bill_input(root, item))
                before = s.get("verified")
                s.update(verify(kind, s["summary"], body))
                if s["verified"] != before:
                    changed += 1
                    print(f"  {s['id']}: {before} → {s['verified']}")
                if not s["verified"]:
                    still += 1
                    for i in s["issues"]:
                        print(f"    {i['kind']} {i['detail']}")
                f.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n判定が変わった {changed}件 / まだ要確認 {still}件")
        return 0

    if args.collect:
        import anthropic
        client = anthropic.Anthropic(api_key=V.load_key(root))
        ids = ([args.collect] if args.collect != "all"
               else sorted(p.stem for p in batch_dir(root).glob("*.json")))
        total = [0, 0, 0]
        for bid in ids:
            print(f"■ {bid}")
            s, f, e = collect(client, root, bid)
            total = [total[0] + s, total[1] + f, total[2] + e]
        print(f"\n保存 {total[0]}件 / 要確認 {total[1]}件 / 失敗 {total[2]}件")
        return 0

    items = targets(root, args.regenerate)
    if args.only:
        items = [x for x in items if x[0] == args.only]
    if args.limit:
        items = items[: args.limit]

    th = sum(1 for k, _, _ in items if k == "thread")
    chars = sum(len(b) for _, _, b in items)
    print(f"■ 対象 {len(items)}件（一般質問 {th} / 議案 {len(items) - th}）")
    print(f"  入力 {chars:,}字  モデル {MODEL}  プロンプト {P.PROMPT_VERSION}")

    if not items:
        print("  未生成のものはありません。")
        return 0
    if args.dry_run:
        print("\n（--dry-run）送信していません。")
        return 0

    import anthropic
    client = anthropic.Anthropic(api_key=V.load_key(root))

    batch_ids = [submit(client, root, items[i:i + CHUNK], i)
                 for i in range(0, len(items), CHUNK)]

    if args.no_wait:
        print("\n完了後に --collect で回収してください。")
        return 0

    saved = flagged = failed = 0
    for bid in batch_ids:
        print(f"■ {bid} の完了を待ちます")
        wait(client, bid)
        s, f, e = collect(client, root, bid)
        saved, flagged, failed = saved + s, flagged + f, failed + e

    print(f"\n保存 {saved}件 / 要確認 {flagged}件 / 失敗 {failed}件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
