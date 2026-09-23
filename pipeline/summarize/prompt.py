"""要約のプロンプトと出力形式。

`docs/設計ドラフト.md`「7. 要約の単位 → プロンプトの原則」と
CLAUDE.md の中立性ルールをそのまま条文にしたもの。

**PROMPT_VERSION を要約JSONに記録する。** 要約は再生成しない方針なので、
あとから「どの版のプロンプトで作った要約か」が分からないと直しようがなくなる。
プロンプトを変えたら必ず上げること。

出力はツール（JSON Schema）で受け取る。地の文でJSONを書かせると、
前置きが混じったり途中で切れたりして、その分だけ再生成が要る。
"""

from __future__ import annotations

PROMPT_VERSION = "2026-09-23.2"

SYSTEM = """あなたは、日本の町議会の会議録を、その町に住む人が読めるように要約します。

要約は「遠賀町議会でこういうやりとりがあった」という事実の記録です。
読む人はこの要約を手がかりに原文へ進みます。原文へ進まない人もいます。
どちらの読者にも誤解を与えないことが、簡潔さより優先します。

## 必ず守ること

1. **発言者が言っていないことを書かない。** 背景の補足、一般論、言い換えによる
   意味の追加をしない。会議録に書かれていないことは、たとえ事実として正しくても書かない。
2. **評価・感想・推測を書かない。** 議員・会派・町・答弁の内容について、
   良し悪し、前向き・後ろ向き、積極的・消極的といった評価語を使わない。
   点数付け・順位付けをしない。議員個人の賛否を推測しない
   （会議録に明記されている賛否はそのまま書いてよい）。
3. **数字・金額・日付・固有名詞は原文のとおりに書く。** 概数に丸めない。
   単位を換算しない。「約」を足さない。原文が「126戸」なら「126戸」と書く。
4. **答弁は結論を明確にする。** 「する」「しない」「検討する」「現時点では答えられない」の
   どれなのかが分かるように書く。答弁が曖昧なまま終わっているなら、
   明確にせず「明確な回答はなかった」と事実として書く。**答えを補わない。**
5. **根拠の発言IDを必ず付ける。** 各要点について、その内容の出どころとなる発言の
   `[番号]` を挙げる。挙げられない要点は、書いてはいけない要点です。
   挙げるのは**質問者と答弁者の発言**だけです。議長の「○○議員。」のような
   進行の発言は根拠になりません。
6. **年の表記を変えない。** 原文が「令和３年度」なら「令和３年度」と書きます。
   西暦に直さない。逆に、原文が西暦なら西暦のままにする。
   **原文にない年を計算して書かない。**

## 書き方

- 「です・ます」で書く。
- 役職は原文のまま使う（町長、建設課長、教育長など）。
- 専門用語はそのまま使い、噛み砕かない。原文に説明があればそれを使う。
- 一つの要点は2〜4文にとどめる。"""

_HUIDS = {
    "type": "array",
    "items": {"type": "integer"},
    "description": "この内容の根拠となる発言の[番号]。本文中の番号だけを使うこと。",
}

THREAD_TOOL = {
    "name": "record_summary",
    "description": "一般質問の要約を、通告書の質問事項ごとに分けて記録する。",
    "input_schema": {
        "type": "object",
        "properties": {
            "topics": {
                "type": "array",
                "description": (
                    "通告書の質問事項と**1対1**にする。通告書が3項目なら、この配列も必ず3件。"
                    "項目を分割して増やしたり、まとめて減らしたりしない。"
                    "小問（(1)(2)(3)）や再質問は、その項目の points に入れる。"
                    "通告書が示されていない場合のみ、やりとりから項目を立てる。"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "no": {"type": "integer", "description": "通告書の質問事項の番号"},
                        "title": {"type": "string", "description": "質問事項の文言（通告書のまま）"},
                        "points": {
                            "type": "array",
                            "description": (
                                "この質問事項の中での、質問と答弁のやりとり。"
                                "小問や再質問ごとに1件。話題が変わったところで区切る。"
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "question": {"type": "string", "description": "議員が尋ねたこと"},
                                    "answer": {
                                        "type": "string",
                                        "description": "町の答弁。誰が答えたか（町長・課長など）と、結論を含める",
                                    },
                                    "huids": _HUIDS,
                                },
                                "required": ["question", "answer", "huids"],
                            },
                        },
                    },
                    "required": ["no", "title", "points"],
                },
            }
        },
        "required": ["topics"],
    },
}

BILL_TOOL = {
    "name": "record_summary",
    "description": "議案について会議録に記録されたやりとりを要約する。",
    "input_schema": {
        "type": "object",
        "properties": {
            "points": {
                "type": "array",
                "description": (
                    "やりとりの要点。提案理由・質疑・討論を分けて並べる。"
                    "該当するやりとりがない種別は、項目そのものを作らない。"
                    "提案理由は1件にまとめる。"
                    "**採決の結果・賛否の数は書かない**（別に記録しているため）。"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["提案理由", "質疑", "討論"],
                            "description": "討論は賛成・反対の意見表明、質疑は内容の確認",
                        },
                        "text": {"type": "string"},
                        "huids": _HUIDS,
                    },
                    "required": ["kind", "text", "huids"],
                },
            }
        },
        "required": ["points"],
    },
}

TOOLS = {"thread": THREAD_TOOL, "bill": BILL_TOOL}


def build(kind: str, body: str, model: str, max_tokens: int = 4000) -> dict:
    """Messages API に渡すパラメータを作る。Batch API のリクエストにもそのまま使える。"""
    tool = TOOLS[kind]
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": SYSTEM,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": tool["name"]},
        "messages": [{"role": "user", "content": body}],
    }
