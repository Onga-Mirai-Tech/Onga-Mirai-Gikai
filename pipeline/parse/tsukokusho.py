"""一般質問通告書（PDF）から、質問者と質問事項を取り出す。

通告書は会議録から復元できない情報を持つ。**質問事項（大項目）が議員自身の
言葉で書かれている**ので、一般質問の要約を「質問事項ごと」に切るための正解に使う。

    一般質問事項（令和８年第５回遠賀町議会９月定例会）
    ◆令和８年９月４日（金）
    （通告順１）質問者 仲野 新三郎 議員
     質問事項 質問の要旨 質問の相手
    １  高齢者支援について ⑴ 現在、遠賀町の第１号被保険者の…

質問者の書き方は2通りある（全83件で確認）。

    令和期  （通告順１）質問者 仲野 新三郎 議員
    平成期  １．◎質問者 萩本 悦子 議員

**11件は pypdf がフォントの符号化（/UniJIS-UTF16-H）を解けず文字化けする。**
その場合は PDF のテキスト描画命令から直接読む（`_extract_via_operators`）。
こちらのほうが行の区切りも素直に出る。

PDFの表はレイアウトの情報が落ちるため、他の工程より精度が落ちる。
読み取れなかった通告書は一覧に出し、その会議は「通告書なし」として扱う。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pypdf

_ZEN_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

# テキスト描画命令。TJ/Tj が文字、Td/TD/T* が改行。
_TOKEN_RE = re.compile(rb"\[(.*?)\]\s*TJ|<([0-9A-Fa-f\s]+)>\s*Tj|(T\*|Td|TD|ET)", re.S)
_HEX_RE = re.compile(rb"<([0-9A-Fa-f\s]+)>")

# 質問者の見出し
# 描画命令から読んだ本文は1文字ずつ改行が入るため、見出しの途中で改行をまたぐ。
# 氏名の部分は改行を含みうるものとして拾い、あとで空白を詰める。
_Q_REIWA_RE = re.compile(r"[（(]\s*通告順\s*([０-９0-9]+)\s*[）)]\s*質問者\s*([\s\S]{1,24}?)\s*議員")
_Q_HEISEI_RE = re.compile(r"([０-９0-9]+)\s*[．.]\s*◎?\s*質問者\s*([\s\S]{1,24}?)\s*議員")
# 会議名
_MEETING_RE = re.compile(r"一\s*般\s*質\s*問\s*事\s*項\s*[（(]([^）)]+)[）)]")
# 質問の要旨の記号。これが出たら質問事項の見出しは終わり。
_POINT_RE = re.compile(r"^\s*(?:[（(]\s*[０-９0-9]+\s*[）)]|[⑴-⒇]|[①-⑳])")
# 質問事項の番号だけの行、または番号＋見出しの行
_ITEM_RE = re.compile(r"^\s*([０-９0-9]{1,2})\s*(?:[\s　]+(.*))?$")
_TAIL_NOISE_RE = re.compile(r"(質問事項|質問の要旨|質問の相手|町\s*長|教育長|・)+\s*$")


@dataclass
class Topic:
    no: int
    title: str


@dataclass
class Notice:
    order: int
    questioner: str
    topics: list[Topic] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)  # 連番から外れて捨てた見出し


@dataclass
class Tsukokusho:
    path: str
    meeting: str
    notices: list[Notice] = field(default_factory=list)
    ok: bool = True
    note: str = ""


def _extract_via_pypdf(path: Path) -> str:
    return "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(str(path)).pages)


def _decode_hex(h: bytes) -> str:
    return bytes.fromhex(re.sub(rb"\s", b"", h).decode()).decode("utf-16-be", errors="replace")


def _extract_via_operators(path: Path) -> str:
    """テキスト描画命令から直接読む。

    pypdf が /UniJIS-UTF16-H を解けない11件のための逃げ道。
    文字列オペランドの中身がそのまま UTF-16BE なので、直接デコードできる。
    """
    out: list[str] = []
    for page in pypdf.PdfReader(str(path)).pages:
        contents = page.get_contents()
        if contents is None:
            continue
        for m in _TOKEN_RE.finditer(contents.get_data()):
            if m.group(1) is not None:
                out.append("".join(_decode_hex(h) for h in _HEX_RE.findall(m.group(1))))
            elif m.group(2) is not None:
                out.append(_decode_hex(m.group(2)))
            else:
                out.append("\n")
    return _rejoin_short_lines(re.sub(r"\n{2,}", "\n", "".join(out)))


def _rejoin_short_lines(text: str) -> str:
    """描画命令から読むと、括弧や数字が1文字ずつ別の行になる。

        （        ←ここで切れると「1」が質問事項の番号に見えてしまう
        1
        ）
        ７月５日～７日にかけての豪雨時対応について

    質問事項の番号は「番号だけの行」で表されるので、この崩れをそのままにすると
    要旨の番号や文中の数字（平成33年度など）まで番号として拾ってしまう。
    2文字以下の行は前の行に繋ぎ直す。
    """
    lines: list[str] = []
    for raw in text.split("\n"):
        line = raw.rstrip()
        if lines and len(line.strip()) <= 2 and not re.fullmatch(r"\s*[０-９0-9]{1,2}\s*", line):
            lines[-1] += line.strip()
        elif lines and len(lines[-1].strip()) <= 2 and not re.fullmatch(r"\s*[０-９0-9]{1,2}\s*", lines[-1]):
            lines[-1] += line.strip()
        else:
            lines.append(line)
    return "\n".join(lines)


def extract_text(path: Path) -> tuple[str, bool]:
    """本文を取り出す。第2要素は「逃げ道を使ったか」。"""
    try:
        text = _extract_via_pypdf(path)
    except Exception:
        text = ""
    if "質問事項" in text:
        return text, False
    return _extract_via_operators(path), True


def _clean_title(raw: str) -> str:
    """見出しの掃除。PDFは折り返しで字間に空白が入るので詰める。"""
    s = re.sub(r"[\s　]+", "", raw)
    s = _TAIL_NOISE_RE.sub("", s)
    return s.strip("・　 ")


def parse_topics(block: str) -> list[Topic]:
    """1人分のブロックから質問事項（大項目）を取り出す。"""
    topics: list[Topic] = []
    current_no: int | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal current_no, buf
        if current_no is not None:
            title = _clean_title("".join(buf))
            if title:
                topics.append(Topic(no=current_no, title=title))
        current_no, buf = None, []

    for line in block.split("\n"):
        if _POINT_RE.match(line):
            # 質問の要旨に入った。見出しは確定。
            flush()
            continue
        m = _ITEM_RE.match(line)
        if m and not buf:
            # 番号だけの行、または番号＋見出しの行
            flush()
            current_no = int(m.group(1).translate(_ZEN_DIGITS))
            rest = m.group(2) or ""
            # 番号・見出し・最初の要旨が同じ行に入ることがある。要旨の手前で切る。
            parts = re.split(r"[（(]\s*[０-９0-9]+\s*[）)]|[⑴-⒇]|[①-⑳]", rest, maxsplit=1)
            buf = [parts[0]]
            if len(parts) > 1:
                flush()
            continue
        if m and buf:
            flush()
            current_no = int(m.group(1).translate(_ZEN_DIGITS))
            buf = [m.group(2) or ""]
            continue
        if current_no is not None:
            # 見出しの折り返し。要旨の記号が同じ行に続く場合はそこで切る。
            parts = re.split(r"[（(]\s*[０-９0-9]+\s*[）)]|[⑴-⒇]|[①-⑳]", line, maxsplit=1)
            buf.append(parts[0])
            if len(parts) > 1:
                flush()
    flush()
    return topics


def parse(path: Path) -> Tsukokusho:
    text, used_fallback = extract_text(path)
    doc = Tsukokusho(path=path.name, meeting="", note="描画命令から読んだ" if used_fallback else "")

    mm = _MEETING_RE.search(text)
    doc.meeting = re.sub(r"[\s　]+", "", mm.group(1)) if mm else ""

    heads = [(int(m.group(1).translate(_ZEN_DIGITS)), re.sub(r"[\s　]+", "", m.group(2)), m.start(), m.end())
             for m in _Q_REIWA_RE.finditer(text)]
    if not heads:
        heads = [(int(m.group(1).translate(_ZEN_DIGITS)), re.sub(r"[\s　]+", "", m.group(2)), m.start(), m.end())
                 for m in _Q_HEISEI_RE.finditer(text)]
    if not heads:
        doc.ok = False
        doc.note = (doc.note + " / " if doc.note else "") + "質問者の見出しを拾えなかった"
        return doc

    for i, (order, name, _s, e) in enumerate(heads):
        end = heads[i + 1][2] if i + 1 < len(heads) else len(text)
        kept, dropped = _keep_sequential(parse_topics(text[e:end]))
        doc.notices.append(Notice(order=order, questioner=name, topics=kept, dropped=dropped))
    if any(n.dropped for n in doc.notices):
        doc.note = (doc.note + " / " if doc.note else "") + "連番から外れた見出しを捨てた"
    return doc


def _keep_sequential(topics: list[Topic]) -> tuple[list[Topic], list[str]]:
    """質問事項の番号は1から連番になる。外れたものは捨てる。

    描画命令から読んだ本文では、文中の数字（「平成33／年度のごみ削減目標…」）が
    行頭に来て番号に見えることがある。連番でないものを落とすことで取り除く。
    捨てた分は記録して、人が確認できるようにする。
    """
    kept: list[Topic] = []
    dropped: list[str] = []
    expected = 1
    for t in topics:
        if t.no == expected:
            kept.append(t)
            expected += 1
        else:
            dropped.append(f"{t.no}. {t.title[:40]}")
    return kept, dropped
