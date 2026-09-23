"""要約モデルに渡す入力テキストを組み立てる。

原文そのものを渡す。要約の要約は作らない（`docs/設計ドラフト.md`「7. 要約の単位」）。

発言には `[huid]` を付ける。要約の各要点に根拠となる発言IDを出させ、
検証（`pipeline/verify`）とサイト上の原文リンクの両方でこれを使うため。
HUIDは会議録側のアンカー番号なので、こちらで採番したIDより原文に辿りやすい。

入力の単位と出力の単位は違う。一般質問は**議員ごとにまとめて**渡し、
**質問事項ごとに分けて**出させる。再質問がテーマをまたぐことがあり、
テーマで切ってから渡すと文脈が切れるため。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pipeline.common.http import decode_cp932
from pipeline.parse import transcript as T


@lru_cache(maxsize=64)
def load_transcript(root: str, unid: str) -> T.Transcript:
    """会議録1日分を読む。原文HTMLはその都度パースする（保存形式を増やさない）。"""
    path = Path(root) / "data" / "raw" / "voices" / f"{unid}.html"
    return T.parse(decode_cp932(path.read_bytes()), unid)


def utterances_by_huid(root: Path, unid: str) -> dict[int, T.Utterance]:
    return {u.huid: u for u in load_transcript(str(root), unid).utterances}


def _speaker(u: T.Utterance) -> str:
    """「答弁者 町長 古野修」のように、立場と役職と名前を並べる。

    誰が答えたのかは要約の要になる。町長の答弁と課長の答弁は重みが違う。
    """
    parts = [u.kind]
    if u.title:
        parts.append(u.title)
    if u.name:
        parts.append(u.name)
    return " ".join(parts)


def _turns(root: Path, unid: str, huids: list[int]) -> list[str]:
    by_huid = utterances_by_huid(root, unid)
    out = []
    for h in huids:
        u = by_huid.get(h)
        if u is None or not u.text.strip():
            continue
        out.append(f"[{h}] {_speaker(u)}\n{u.text.strip()}")
    return out


def thread_input(root: Path, thread: dict) -> str:
    """一般質問1人分。通告書の質問事項を添えて渡す。

    通告書は質問事項の一覧と境界の正解を持っている。これを渡すことで、
    本文からテーマの切れ目を機械判定する必要がなくなる。
    """
    head = [
        f"【一般質問】{thread['meeting']}　{thread['on']}",
        f"質問者: {thread['questioner']}（{thread['questioner_title']}）",
    ]
    if thread.get("topics"):
        head.append("")
        head.append("一般質問通告書に記載された質問事項:")
        # 通告書の項目は {"no": 1, "title": "..."}。通告書の番号をそのまま使う。
        # こちらで振り直すと、要約と通告書の突き合わせ（検証）ができなくなる。
        head += [f"  {t['no']}. {t['title']}" for t in thread["topics"]]
    else:
        # 通告書と対応づかないスレッドがある（`note` に理由が入る）。
        # その場合は質問事項を与えず、本文から立てさせる。
        head.append("")
        head.append("※ この質問については通告書が見つかっていません。")

    body = _turns(root, thread["unid"], [t["huid"] for t in thread["turns"]])
    return "\n".join(head) + "\n\n会議録:\n\n" + "\n\n".join(body) + "\n"


def bill_input(root: Path, bill: dict) -> str:
    """議案1件分。日程をまたぐ場合は全部まとめて渡す。"""
    head = [
        f"【議案】{bill['meeting']}",
        f"{bill['number']}　{bill['title']}",
    ]
    if bill.get("result"):
        head.append(f"議決結果: {bill['result']}")
    if bill.get("committees"):
        head.append(f"付託: {'・'.join(bill['committees'])}")

    body: list[str] = []
    for unid in sorted({u["unid"] for u in bill["utterances"]}):
        huids = [u["huid"] for u in bill["utterances"] if u["unid"] == unid]
        body += _turns(root, unid, sorted(huids))
    return "\n".join(head) + "\n\n会議録:\n\n" + "\n\n".join(body) + "\n"


def load_normalized(root: Path) -> tuple[list[dict], list[dict]]:
    norm = root / "data" / "normalized"
    threads = json.loads((norm / "threads.json").read_text(encoding="utf-8"))
    bills = json.loads((norm / "bills.json").read_text(encoding="utf-8"))
    return threads, bills
