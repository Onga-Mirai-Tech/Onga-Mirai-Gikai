"""ふりがな（ルビ）を付ける（2026-11-18）。

**付けるのは、このサイトが書いた文（説明文・見出し・案内）とAI要約だけ。**
会議録の発言、一般質問通告書の質問事項、議案名、会議名、人名など、
公式資料から持ってきた文字には付けない。自動で付けた読みが公式の記録の上に載ると、
読みの誤りが原文の誤りのように見えるため。

見分け方: 公式資料の文字は `site.esc()` を通すときに目印（私用領域の2文字）で囲む。
ページを組み立てたあと、目印の外にある漢字にだけ読みを付け、目印を取り除く。
目印を付け忘れた場合は**付けない側に倒れない**ので、公式資料の文字は必ず
`esc()` を通すこと（このサイトでは以前からそうしている）。

読みは形態素解析（Sudachi）で出す。誤りやすいものは次のように扱う。
- 固有名詞（人名・地名・団体名）は**自動の読みを使わない**。
  人が確かめた読みの一覧（masters/furigana.json）にあるものだけに付ける
- 議員・町長などの氏名（発言者マスタにあるもの）は、AI要約の中に出てきても付けない
- 数字のすぐ後ろの「日」「分」「本」などは、読みが数字によって変わる（10日＝とおか）ので付けない

ふりがなは、ふだんは表示しない。ページ上部の「ふりがな」ボタンで切り替える（site/assets/furigana.js）。
"""

from __future__ import annotations

import functools
import re

# 公式資料の文字を囲む目印。Unicode の私用領域なので、会議録や通告書には出てこない。
SRC_OPEN = ""
SRC_CLOSE = ""

_KANJI = "㐀-䶿一-鿿豈-﫿々〆"
_KANJI_RE = re.compile(f"[{_KANJI}]")
_RUN_RE = re.compile(f"[{_KANJI}]+|[^{_KANJI}]+")
_DIGIT_RE = re.compile(r"[0-9０-９]$")

# 中に読みを入れてはいけない要素。<option> の中にタグを書くと、ブラウザは
# タグを捨てて文字だけを残すので「議案ぎあん」と並んでしまう。
_SKIP_TAGS = {"head", "title", "script", "style", "option", "select", "textarea", "ruby", "svg"}
_TAG_RE = re.compile(r"(<[^>]*>)")
_TAG_NAME_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z0-9]+)")
_VOID = {"meta", "link", "br", "img", "input", "hr", "source", "wbr"}

# 数字のすぐ後ろでは、数字によって読みが変わる助数詞。
# 例: 1日（ついたち）・10日（とおか）、1分（いっぷん）・3本（さんぼん）、1人（ひとり）。
# 解析器は前の数字を見ずに読むことがあるので、ここには付けない。
# 「件」「回」「年」「月」のように、どの数字でも読みが同じものには付ける。
_VARIES_AFTER_DIGIT = {"日", "分", "本", "杯", "匹", "発", "泊", "階", "軒", "足", "人",
                       "羽", "頭", "冊", "着", "百", "千", "日間", "人分"}

# 文脈で読みが分かれ、解析器がよく取り違える語。付けない（2026-11-19、生成結果を見て決めた）。
#   方   「お持ちの方」はかた、「こちらの方が」はほう。人を指す「かた」を「ほう」と読みがち
#   行っ 「行っています」はおこなって、「説明を聞きに行った」はいった
#   主   「〜が主（おも）」を「ぬし」と読みがち
#   付   「3月31日付（づけ）」「プレミアム付（つき）」を「つけ」と読みがち
#   何   なに／なん
#   小   「広渡小（しょう）」を「さ」と読みがち。こ／お とも読む
_AMBIGUOUS = {"方", "行っ", "主", "付", "何", "小"}

# 姓のすぐ後ろに続く役職。地名と同じ姓（田代）が「田代議員」と出てきたら人名として扱う。
_TITLE_RE = re.compile(r"議員|町長|副町長|教育長|議長|副議長|委員長|副委員長|課長|部長|局長|室長|係長|所長|氏|さん|君")


def mark_source(s: str) -> str:
    """公式資料の文字に目印を付ける（ふりがなを付けない）。"""
    return f"{SRC_OPEN}{s}{SRC_CLOSE}"


def strip_marks(s: str) -> str:
    return s.replace(SRC_OPEN, "").replace(SRC_CLOSE, "")


def to_hiragana(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def align(surface: str, reading: str) -> list[tuple[str, str | None]] | None:
    """語と読みを、漢字の部分と仮名の部分に分けて対応させる。

    「話さ」「はなさ」→ [("話", "はな"), ("さ", None)]
    「取り組み」「とりくみ」→ [("取", "と"), ("り", None), ("組", "く"), ("み", None)]
    合わないときは None（読みを付けない）。送り仮名の上に読みを重ねないため。
    """
    parts = _RUN_RE.findall(surface)
    if len(parts) == 1 and _KANJI_RE.match(parts[0]):
        return [(surface, reading)]
    pattern = "".join("(.+?)" if _KANJI_RE.match(p) else re.escape(to_hiragana(p)) for p in parts)
    m = re.fullmatch(pattern, reading)
    if not m:
        return None
    groups = iter(m.groups())
    return [(p, next(groups)) if _KANJI_RE.match(p) else (p, None) for p in parts]


@functools.lru_cache(maxsize=1)
def tokenizer():
    """形態素解析器。辞書の読み込みに1秒ほどかかるので、1回だけ作る。

    分け方は中くらいの単位（B）。最も長い単位（C）だと「男女共同参画社会」に
    17文字の読みが一続きに付き、読みが漢字より長くなって字の間が広がる。
    最も短い単位（A）だと「本棚」が「本＋棚（たな）」になり、濁る読みを失う。
    """
    from sudachipy import Dictionary, SplitMode
    return Dictionary(dict="core").tokenizer(mode=SplitMode.B)


def ruby(base: str, reading: str) -> str:
    return f"<ruby>{base}<rt>{reading}</rt></ruby>"


class Furigana:
    """`fixed` は人が確かめた読み（語 → ひらがな）、`skip` は読みを付けない語（氏名など）。

    `skip` と `fixed` の両方にある語（地名と同じ姓）は、後ろに役職が続くときだけ付けない。
    """

    def __init__(self, fixed: dict[str, str] | None = None, skip: set[str] | None = None):
        self.fixed = dict(fixed or {})
        skip = {w for w in (skip or set()) if w}
        self.name_like = skip & set(self.fixed)
        self.skip = skip - set(self.fixed)
        words = sorted(set(self.fixed) | self.skip, key=len, reverse=True)
        self._words_re = re.compile("|".join(map(re.escape, words))) if words else None

    @functools.lru_cache(maxsize=None)
    def text(self, s: str) -> str:
        """HTMLの文字の部分（エスケープ済み・タグなし）に読みを付ける。"""
        if not _KANJI_RE.search(s):
            return s
        if self._words_re is None:
            return self._analyze(s)
        out, pos = [], 0
        for m in self._words_re.finditer(s):
            out.append(self._analyze(s[pos:m.start()]))
            w = m.group()
            as_name = w in self.name_like and _TITLE_RE.match(s, m.end())
            out.append(self._fixed(w) if w in self.fixed and not as_name else w)
            pos = m.end()
        out.append(self._analyze(s[pos:]))
        return "".join(out)

    def _fixed(self, w: str) -> str:
        """人が確かめた読み。「ふるさと納税」のように仮名を含む語は、漢字の上にだけ付ける。"""
        pairs = align(w, self.fixed[w])
        if pairs is None:
            return ruby(w, self.fixed[w])
        return "".join(ruby(b, r) if r else b for b, r in pairs)

    def _analyze(self, s: str) -> str:
        if not _KANJI_RE.search(s):
            return s
        tokens = tokenizer().tokenize(s)
        if "".join(t.surface() for t in tokens) != s:
            return s  # 元の文字を一字でも変えるくらいなら、付けない
        out = []
        prev = ""
        for t in tokens:
            surface = t.surface()
            out.append(self._token(surface, t.reading_form(), t.part_of_speech(), prev))
            prev = surface
        return "".join(out)

    def _token(self, surface: str, reading: str, pos: tuple, prev: str) -> str:
        if not _KANJI_RE.search(surface):
            return surface
        if pos[1] == "固有名詞":
            return surface  # 人名・地名・団体名は、確かめた読み（fixed）だけ
        if _DIGIT_RE.search(prev) and surface in _VARIES_AFTER_DIGIT:
            return surface
        if surface in _AMBIGUOUS:
            return surface
        hira = to_hiragana(reading)
        if not hira or _KANJI_RE.search(hira) or hira == surface or not re.fullmatch(r"[ぁ-ゖー]+", hira):
            return surface  # 読みが辞書になかった（未知語）
        pairs = align(surface, hira)
        if pairs is None:
            return surface
        return "".join(ruby(b, r) if r else b for b, r in pairs)

    def annotate(self, page_html: str) -> str:
        """ページ全体に読みを付け、目印を取り除く。

        タグの中（属性）と、_SKIP_TAGS の要素の中、目印で囲んだ部分には付けない。
        """
        out = []
        stack: list[str] = []
        in_source = False
        for piece in _TAG_RE.split(page_html):
            if not piece:
                continue
            if piece.startswith("<"):
                m = _TAG_NAME_RE.match(piece)
                if m:
                    closing, name = m.group(1), m.group(2).lower()
                    if name in _SKIP_TAGS and name not in _VOID:
                        if closing:
                            if name in stack:
                                del stack[len(stack) - 1 - stack[::-1].index(name):]
                        elif not piece.endswith("/>"):
                            stack.append(name)
                out.append(strip_marks(piece))
                continue
            if stack:
                out.append(strip_marks(piece))
                continue
            # 文字の部分。目印の内と外を切り分ける
            buf = []
            for chunk in re.split(f"([{SRC_OPEN}{SRC_CLOSE}])", piece):
                if chunk == SRC_OPEN:
                    in_source = True
                elif chunk == SRC_CLOSE:
                    in_source = False
                elif chunk:
                    buf.append(chunk if in_source else self.text(chunk))
            text = "".join(buf)
            # 読みを付けた文字は <span> でひとまとめにする。見出し（h2）などは横並び（flex）で、
            # 文字のままなら1つの部品だが、<ruby> が混ざると語ごとに別の部品になり、間があく。
            out.append(f"<span>{text}</span>" if "<ruby>" in text else text)
        return "".join(out)
