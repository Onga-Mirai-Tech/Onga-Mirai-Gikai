"""町公式HPの「審議案件・結果」PDFを読む。

会議録から取り出した議決結果を照合するための**独立した第二の情報源**
（`docs/設計ドラフト.md`「検証（verify）」）。会議録の読み取りを直す目的にだけ使い、
サイトに載せる議決結果は会議録本文から取ったものを使う。

## 文字化けへの対処

このPDFは Type0 / Identity-H のサブセットフォントを使っており、
pypdf の `extract_text()` は**グリフ番号をそのまま文字として返す**。
「令和」が「௧࿴」になるのはこのため。

フォントに `/ToUnicode` 変換表が入っているので、内容ストリームから
`TJ` の生バイトを取り出し、その時点のフォントの変換表で引き直す。
一般質問通告書（`tsukokusho.py`）とは化け方が違うので、別に書いている。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pypdf

_ZEN_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

# 変換表にないCIDの印。照合の結果に出てきたら、抽出側を直す合図。
UNKNOWN = "〓"

# 議案の種別。会議録側（pipeline/parse/bills.py）と同じ語を使う。
_NUMBER_RE = re.compile(r"^(議案|発議|発委|報告|請願|陳情|意見書案|諮問|承認)第\s*(\d{1,3})\s*号")
# **長いものから順に見る。** 「不採択」より先に「採択」を試すと、
# 「不採択」が「採択」と読まれ、賛否が逆になる（実測: 請願第1号・陳情第1号）。
_RESULT_WORDS = tuple(sorted((
    "原案可決", "修正可決", "可決", "否決", "承認", "不承認", "同意", "不同意",
    "認定", "不認定", "採択", "不採択", "適任", "撤回", "継続審査", "取下げ",
), key=len, reverse=True))


@dataclass(frozen=True)
class Item:
    """PDFの1行。会議録側の議案と突き合わせる単位。"""

    number: str  # 「議案第42号」
    title: str
    result: str  # 「原案可決」など
    decided_on: str  # 「2025-03-21」。読めなければ空


def _unescape(b: bytes) -> bytes:
    """PDFのリテラル文字列のエスケープを戻す。"""
    out = bytearray()
    i = 0
    while i < len(b):
        if b[i : i + 1] == b"\\" and i + 1 < len(b):
            nxt = b[i + 1 : i + 2]
            if nxt.isdigit():
                j = i + 1
                while j < len(b) and j < i + 4 and b[j : j + 1].isdigit():
                    j += 1
                out.append(int(b[i + 1 : j], 8) & 0xFF)
                i = j
                continue
            out += {b"n": b"\n", b"r": b"\r", b"t": b"\t"}.get(nxt, nxt)
            i += 2
            continue
        out += b[i : i + 1]
        i += 1
    return bytes(out)


_BFCHAR_RE = re.compile(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")
_BFRANGE_RE = re.compile(
    r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(?:<([0-9A-Fa-f]+)>|\[([^\]]*)\])"
)


def _tounicode(font) -> dict[int, str]:
    """フォントの /ToUnicode 変換表を読む。ないフォントは空。

    `bfrange` には2つの形がある。両方を読まないと文字が欠ける。

        <0F0A> <0F0B> <533A>            連番。先頭から1つずつ増やす
        <0F20> <0F21> [<5354> <5357>]   1つずつ列挙。連番ではない

    後者を読み落とすと「固定資産」が「定資産」になる（実際に起きた）。
    """
    if "/ToUnicode" not in font:
        return {}
    text = font["/ToUnicode"].get_object().get_data().decode("latin-1")
    out: dict[int, str] = {}

    for blk in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in _BFCHAR_RE.findall(blk):
            out[int(src, 16)] = bytes.fromhex(dst).decode("utf-16-be", "replace")

    for blk in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, dst, arr in _BFRANGE_RE.findall(blk):
            start, end = int(lo, 16), int(hi, 16)
            if arr:
                vals = re.findall(r"<([0-9A-Fa-f]+)>", arr)
                for code, v in zip(range(start, end + 1), vals):
                    out[code] = bytes.fromhex(v).decode("utf-16-be", "replace")
                continue
            if len(dst) > 4:  # 1文字に複数コードの割り当ては扱わない
                continue
            base = int(dst, 16)
            for i, code in enumerate(range(start, end + 1)):
                out[code] = chr(base + i)
    return out


_TOKEN_RE = re.compile(rb"/(F\d+)[^\n]*?Tf|\[(.*?)\]\s*TJ|\((?:\\.|[^()\\])*\)\s*Tj", re.S)
_PART_RE = re.compile(rb"\(((?:\\.|[^()\\])*)\)|<([0-9A-Fa-f]+)>", re.S)


def extract_text(path: Path) -> str:
    """PDFから文字を取り出す。フォントごとの変換表で引き直す。"""
    reader = pypdf.PdfReader(str(path))
    lines: list[str] = []

    for page in reader.pages:
        fonts = page.get("/Resources", {}).get("/Font", {})
        maps = {name.lstrip("/"): _tounicode(ref.get_object()) for name, ref in fonts.items()}
        current: dict[int, str] = {}
        buf: list[str] = []

        for m in _TOKEN_RE.finditer(page.get_contents().get_data()):
            if m.group(1):
                current = maps.get(m.group(1).decode(), {})
                continue
            body = m.group(2) if m.group(2) is not None else m.group(0)
            # findall は参加しなかったグループに **空バイト列**を返す（None ではない）。
            # `lit is not None` で分けると、16進表記の文字列を丸ごと落とす。
            data = b"".join(
                _unescape(lit) if lit else (bytes.fromhex(hx.decode()) if hx else b"")
                for lit, hx in _PART_RE.findall(body)
            )
            if not current:
                # 変換表を持たないフォント（英数字用）。そのまま読む。
                buf.append(data.decode("latin-1"))
                continue
            cids = [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data) - 1, 2)]
            # 変換表にないCIDは **黙って捨てない**。照合に使う文字列なので、
            # 欠けたことが分かるように印を残す。捨てると誤った件名が静かにできあがる。
            buf.append("".join(current.get(c, UNKNOWN) for c in cids))

        lines.append("".join(buf))

    return "\n".join(lines)


# 表の「議決年月日」欄。和暦の年だけを数字で書く（「7.3.21」＝令和7年3月21日）。
_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{1,2})$")
_ERA_RE = re.compile(r"(令和|平成)(元|\d{1,2})年")


def _squeeze(text: str) -> str:
    """全角数字を半角にし、空白を落とす。表題は空白で字送りされているため。"""
    return re.sub(r"[ \u3000]+", "", text.translate(_ZEN_DIGITS))
_ERA_BASE = {"令和": 2018, "平成": 1988}


_MEETING_RE = re.compile(r"(令和|平成)(元|\d{1,2})年第(\d{1,2})回[^\n]{0,20}?(定例会|臨時会)")


def meeting_id(text: str) -> str:
    """表題から会議IDを作る。会議録側（`pipeline/parse/bills.py`）と同じ形にする。

    「令和７年第２回遠賀町議会３月定例会」→ "2025-t2"
    表題は会議名の途中に「遠賀町議会」が挟まるので、会議録の表記とは一致しない。
    IDに揃えたうえで突き合わせる。
    """
    m = _MEETING_RE.search(_squeeze(text))
    if not m:
        return ""
    era, yy, no, kind = m.groups()
    year = _ERA_BASE[era] + (1 if yy == "元" else int(yy))
    return f"{year}-{'t' if kind == '定例会' else 'r'}{int(no)}"


def era_base(text: str) -> int | None:
    """表題の「令和７年第２回…」から元号を読む。

    議決年月日の欄には元号が書かれていない（「7.3.21」だけ）。
    年だけを見ても令和7年か平成7年か決められないので、表題から取る。
    """
    m = _ERA_RE.search(_squeeze(text))
    return _ERA_BASE[m.group(1)] if m else None


def split_date(title: str, base: int | None) -> tuple[str, str]:
    """件名の末尾にくっついている議決年月日を切り離す。"""
    m = _DATE_RE.search(title)
    if not m or base is None:
        return title, ""
    y, mo, d = (int(x) for x in m.groups())
    return title[: m.start()].strip(), f"{base + y:04d}-{mo:02d}-{d:02d}"


def _find_result(text: str) -> tuple[str, str] | None:
    """文字列の末尾についている議決結果を切り離す。「…について原案可決」→（…について, 原案可決）"""
    for w in _RESULT_WORDS:
        if text.endswith(w):
            return text[: -len(w)].strip(), w
    return None


def parse(text: str) -> list[Item]:
    """抽出した文字列から議案の行を拾う。

    PDFは「議案番号／件名／議決結果」の表で、番号と件名・結果が
    行として分かれたりくっついたりする。番号を見つけたところで区切り、
    次の番号までを件名＋結果として扱う。
    """
    flat = re.sub(r"[ 　]+", "", text.translate(_ZEN_DIGITS))
    # 番号の前で必ず改行する。表の折り返しで番号が行頭に来ないことがあるため。
    flat = re.sub(r"(?=(議案|発議|発委|報告|請願|陳情|意見書案|諮問|承認)第\d{1,3}号)", "\n", flat)

    base = era_base(text)
    items: list[Item] = []
    for chunk in flat.split("\n"):
        chunk = chunk.strip()
        m = _NUMBER_RE.match(chunk)
        if not m:
            continue
        number = f"{m.group(1)}第{int(m.group(2))}号"
        rest = chunk[m.end():].strip()
        found = _find_result(rest)
        if found is None:
            # 結果が次の行や次のページに回っている。件名だけ拾って結果は空にする。
            title, on = split_date(rest, base)
            items.append(Item(number=number, title=title, result="", decided_on=on))
            continue
        title, result = found
        title, on = split_date(title, base)
        items.append(Item(number=number, title=title, result=result, decided_on=on))
    return items
