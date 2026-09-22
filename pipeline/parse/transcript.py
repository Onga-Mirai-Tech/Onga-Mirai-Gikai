"""会議録1日分（ACT=203 のHTML）を構造化する。

ページの作りは `docs/調査報告_P0.md` の「2. 本文の書式」で実物を確かめたもの。
平成17年から令和8年まで、構造・記号・ラベルがすべて同じなので年代で分岐しない。

    ┌ ヘッダ（最初の HUID ブロック）
    │   令和　８年第　３回定例会
    │   １．議長の氏名
    │   ２．説明のため出席した者の氏名・職
    │   ３．書記の氏名
    │   ４．議員の出欠      ← 罫線素片で組んだ表
    │   議事日程（第２号）
    └ 本文（2つ目以降の HUID ブロック）
        ○議長（織田隆徳）　…      U+25CB 議長・委員長
        ◆３番議員（田代順二）　…  U+25C6 質問者
        ◎税務課長（井口正彦）　…  U+25CE 答弁者
        △日程第１ …               U+25B3 日程の見出し（発言者の記載がない）

記号は検索システムの発言者区分（KTYP）と一対一で対応している。
こちらで解釈した結果ではないので、そのまま使う。
"""

from __future__ import annotations

import html as html_mod
import re
import unicodedata
from dataclasses import dataclass, field

# 発言者の記号。KTYP 1/2/3 に対応する。
MARK_CHAIR = "○"  # ○ 議長・委員長
MARK_ASKER = "◆"  # ◆ 質問者
MARK_ANSWERER = "◎"  # ◎ 答弁者
MARK_AGENDA = "△"  # △ 日程の見出し

MARK_KIND = {
    MARK_CHAIR: "議長",
    MARK_ASKER: "質問者",
    MARK_ANSWERER: "答弁者",
}

_HUID_SPLIT_RE = re.compile(r'<A NAME="HUID(\d+)"></A>', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|form)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_MARKS = f"{MARK_CHAIR}{MARK_ASKER}{MARK_ANSWERER}"
# 「◎税務課長（井口正彦）」が基本形。実物には次の変種がある（全460件で確認）。
#   ◎（堅田繁）                                    職の記載がない
#   ◎遠賀町議会の運営に関する…特別委員会委員長（濱田竜一）  職が30字を超える
_SPEAKER_RE = re.compile(rf"^([{_MARKS}])([^（）\n]{{0,60}})（([^（）\n]{{1,30}})）")
# 括弧を使わず敬称で締める形。参考人・受章者・任命された委員の挨拶に出る。
#   ◎山中巧吉氏　…／◎西村朗さん　…
_SPEAKER_PLAIN_RE = re.compile(rf"^([{_MARKS}])([^\s　（）]{{2,12}}?)(氏|さん|君)(?=[\s　])")
_AGENDA_HEAD_RE = re.compile(rf"^{MARK_AGENDA}\s*(追加)?日程第\s*([０-９0-9]{{1,3}})")
_NOTE_RE = re.compile(r"───\s*(.+?)\s*───")
_SECTION_RE = re.compile(r"^\s*([０-９0-9]{1,2})[．.]\s*(\S.*)$")

_ZEN_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_SPACES = "　 \t"

# 出欠表の記号。見出し行に「（出席 ／・　欠席　△）」と凡例が出る。
ATTEND_PRESENT = "／"
ATTEND_ABSENT = "△"


def to_text(fragment: str) -> str:
    """HTML片を素のテキストにする。<BR> と <P> は改行に置く。"""
    s = _SCRIPT_RE.sub("", fragment)
    s = re.sub(r"<BR\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<P\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = _TAG_RE.sub("", s)
    return html_mod.unescape(s)


def squeeze(text: str) -> str:
    """全角数字を半角にし、空白（全角含む）を落とす。照合用の正規形。

    会議名も氏名も全角スペースで字送りされているので、比較の前に必ず通す。
    """
    return unicodedata.normalize("NFKC", text.translate(_ZEN_DIGITS)).replace(" ", "").strip()


@dataclass
class Person:
    title: str  # 役職。議員の場合は「３番議員」など
    name: str  # 照合用（空白を除いたもの）
    name_raw: str  # 原文のまま


@dataclass
class Attendance:
    seat: str  # 「１番」など。原文のまま
    name: str
    name_raw: str
    status: str  # 出席 | 欠席 | 辞職 | 欠番 | 欠員 | 記載なし | その他


@dataclass
class Utterance:
    huid: int
    seq: int
    mark: str  # ○ ◆ ◎ △ のいずれか、または "" （ヘッダ）
    kind: str  # 議長 | 質問者 | 答弁者 | 日程 | その他
    title: str  # 「議長」「３番議員」「税務課長」など。日程行は ""
    name: str  # 照合用。日程行は ""
    name_raw: str
    text: str  # 発言本文（話者ラベルを除いたもの）
    agenda_no: int | None = None  # △日程第N の N
    notes: list[str] = field(default_factory=list)  # ───　質疑なし　─── の中身


@dataclass
class Transcript:
    unid: str
    title_raw: str  # 「令和　８年第　３回定例会」
    chair_title: str  # 「議長の氏名」または「副議長の氏名」
    chair: Person | None
    executives: list[Person]
    clerks: list[Person]
    attendance: list[Attendance]
    agenda: list[str]
    utterances: list[Utterance]

    @property
    def present_count(self) -> int:
        return sum(1 for a in self.attendance if a.status == "出席")


def _split_blocks(page: str) -> list[tuple[int, str]]:
    """HUIDアンカーで区切る。先頭はヘッダなので huid=-1 を振る。"""
    parts = _HUID_SPLIT_RE.split(page)
    if len(parts) < 3:
        return []
    blocks: list[tuple[int, str]] = []
    for i in range(1, len(parts) - 1, 2):
        blocks.append((int(parts[i]), parts[i + 1]))
    return blocks


# 役職は空白を含まない1語で、「長・役・者・監・員・佐」で終わる。
# 町長 / 副町長 / 教育長 / 収入役 / 助役 / 会計管理者 / 駅周辺都市整備推進室長 /
# 議会事務局長 / 事務係長 など、実物で確認した範囲をすべて拾える。
_JOB_RE = re.compile(r"^[^\s　]*[長役者監員佐]$")

# 氏名と職が空白なしで続く行があるため（「平田　多賀子事務係長」）、
# 職名そのものを探して切る。書記欄に出てくる職はこの5語だけ（全460件で確認）。
# 長いものから並べる。
_JOB_FIND_RE = re.compile(r"議会事務局長|事務局次長|事務係長|主査|書記")


def _split_person_line(body: str) -> list[tuple[str, str]]:
    """1行から (職, 氏名) の組を取り出す。0組なら職だけの行。

    1行に2人ぶん書かれていることがある。
        吉村　哲司　議会事務局長　　平田　多賀子事務係長
    """
    tokens = [t for t in re.split(r"[\s　]+", body) if t]
    if not tokens:
        return []

    if _JOB_RE.match(tokens[0]):
        name = "　".join(tokens[1:])
        return [(tokens[0], name)] if name else []

    spans = list(_JOB_FIND_RE.finditer(body))
    if spans:
        # 氏名 → 職 の順。職の直前までが氏名。
        pairs: list[tuple[str, str]] = []
        cursor = 0
        for m in spans:
            name = body[cursor:m.start()].strip(_SPACES)
            if name:
                pairs.append((m.group(0), name))
            cursor = m.end()
        if pairs:
            return pairs

    if len(tokens) > 1 and _JOB_RE.match(tokens[-1]):
        return [(tokens[-1], "　".join(tokens[:-1]))]

    # 職の語形に当てはまらない。欄の固定幅（全角6文字）で切る。
    name = body[7:].strip(_SPACES) if len(body) > 7 else ""
    return [(body[:6].strip(_SPACES), name)] if name else []


def _parse_person_lines(lines: list[str]) -> list[Person]:
    """「職」と「氏名」の行を読む。

    並び順が時代で違う。**語形で判定し、位置で決め打ちしない。**

        令和  　　　議会事務局長　野口　健治     職 → 氏名
        平成  　　　吉　村　哲　司　議会事務局長  氏名 → 職

    職が長くて欄に収まらないときは、氏名だけが次の行に送られる。

        　　　みらいデザイン課長
        　　　　　　　　　　安部　真介      ← 先頭の空白が深い＝前の行の氏名
    """
    people: list[Person] = []
    pending_title: str | None = None

    for raw in lines:
        if not raw.strip(_SPACES):
            continue
        indent = len(raw) - len(raw.lstrip(_SPACES))
        body = raw.strip(_SPACES)

        if pending_title is not None and indent >= 8:
            people.append(Person(title=pending_title, name=squeeze(body), name_raw=body))
            pending_title = None
            continue
        pending_title = None

        pairs = _split_person_line(body)
        if not pairs:
            # 職だけの行。氏名は次の行に来る。
            pending_title = body
            continue
        for title, name_raw in pairs:
            people.append(Person(title=title, name=squeeze(name_raw), name_raw=name_raw))

    return people


def _parse_attendance(lines: list[str]) -> list[Attendance]:
    """罫線素片で組んだ出欠表を読む。

    1行に3人ぶんが横に並ぶ。空欄や「辞職」「欠員」が入ることがある。
    """
    rows: list[Attendance] = []
    for raw in lines:
        if not raw.startswith("│"):
            continue
        cells = [c.strip(_SPACES) for c in raw.split("│") if c != ""]
        for i in range(0, len(cells) - 2, 3):
            mark, seat, name_raw = cells[i], cells[i + 1], cells[i + 2]
            if not seat or not name_raw:
                continue
            if "氏" in name_raw and "名" in name_raw:  # 見出し行
                continue
            if set(name_raw) <= {"─", "━", "-"}:  # 罫線
                continue
            name = squeeze(name_raw)
            if mark == ATTEND_PRESENT:
                status = "出席"
            elif mark == ATTEND_ABSENT:
                status = "欠席"
            elif not mark:
                # 出欠の欄が空。会議録がそう書いているので、そのまま記録する。
                status = "記載なし"
            else:
                status = "その他"
            if name in ("辞職", "欠員", "欠番", "死去", "空席"):
                status = name
            rows.append(Attendance(seat=seat, name=name, name_raw=name_raw, status=status))
    return rows


def parse_header(block: str) -> dict:
    """先頭のHUIDブロック（ヘッダ）を読む。"""
    lines = to_text(block).split("\n")

    title_raw = next((l.strip(_SPACES) for l in lines if l.strip(_SPACES)), "")

    # 「１．議長の氏名」のような節見出しで区切る
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        m = _SECTION_RE.match(line)
        if m:
            current = m.group(2).strip(_SPACES)
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)

    def find(*keywords: str) -> tuple[str, list[str]]:
        for key, body in sections.items():
            if any(k in key for k in keywords):
                return key, body
        return "", []

    chair_key, chair_lines = find("議長の氏名")
    # 「１．議長の氏名　　　織田　隆徳」と、見出しと氏名が同じ行に書かれている。
    chair_title, _, chair_inline = chair_key.partition("氏名")
    chair_title = (chair_title + "氏名") if chair_title else chair_key
    chair_inline = chair_inline.strip(_SPACES)

    chair_people = _parse_person_lines(chair_lines)
    if not chair_people and chair_inline:
        chair_people = [
            Person(title=chair_title.replace("の氏名", ""), name=squeeze(chair_inline), name_raw=chair_inline)
        ]

    _, exec_lines = find("説明のため出席した者")
    _, clerk_lines = find("書記の氏名")
    attend_key, attend_lines = find("議員の出欠", "議員の出席")

    # 議事日程は節見出しの外にある。出欠表のあとの行から拾う。
    agenda: list[str] = []
    for line in lines:
        s = line.strip(_SPACES)
        if re.match(r"^日程第\s*[０-９0-9]+", s):
            agenda.append(s)

    return {
        "title_raw": title_raw,
        "chair_title": chair_title or "議長の氏名",
        "chair": chair_people[0] if chair_people else None,
        "executives": _parse_person_lines(exec_lines),
        "clerks": _parse_person_lines(clerk_lines),
        "attendance": _parse_attendance(attend_lines or lines),
        "agenda": agenda,
    }


def parse_utterance(huid: int, seq: int, block: str) -> Utterance:
    text = to_text(block).strip("\n")
    stripped = text.lstrip("\n").lstrip(_SPACES)

    notes = [m.strip(_SPACES) for m in _NOTE_RE.findall(text)]

    m = _SPEAKER_RE.match(stripped)
    if m:
        mark, title, name_raw = m.group(1), m.group(2).strip(_SPACES), m.group(3).strip(_SPACES)
        body = stripped[m.end():].lstrip(_SPACES)
        return Utterance(
            huid=huid, seq=seq, mark=mark, kind=MARK_KIND[mark],
            title=title, name=squeeze(name_raw), name_raw=name_raw,
            text=body.strip(), notes=notes,
        )

    m = _SPEAKER_PLAIN_RE.match(stripped)
    if m:
        mark, name, honorific = m.group(1), m.group(2), m.group(3)
        body = stripped[m.end():].lstrip(_SPACES)
        return Utterance(
            huid=huid, seq=seq, mark=mark, kind=MARK_KIND[mark],
            title="", name=squeeze(name), name_raw=name + honorific,
            text=body.strip(), notes=notes,
        )

    m = _AGENDA_HEAD_RE.match(stripped)
    if m:
        body = stripped[m.end():].lstrip(_SPACES)
        return Utterance(
            huid=huid, seq=seq, mark=MARK_AGENDA,
            kind="追加日程" if m.group(1) else "日程",
            title="", name="", name_raw="",
            text=body.strip(), agenda_no=int(m.group(2).translate(_ZEN_DIGITS)), notes=notes,
        )

    return Utterance(
        huid=huid, seq=seq, mark="", kind="その他",
        title="", name="", name_raw="", text=stripped.strip(), notes=notes,
    )


def parse(page: str, unid: str) -> Transcript:
    """1日分のHTMLを構造化する。`page` は CP932 からデコードした文字列。"""
    blocks = _split_blocks(page)
    if not blocks:
        raise ValueError(f"HUIDのアンカーが見つからない: {unid}")

    header = parse_header(blocks[0][1])
    utterances = [
        parse_utterance(huid, seq, block)
        for seq, (huid, block) in enumerate(blocks[1:], start=1)
    ]

    return Transcript(
        unid=unid,
        title_raw=header["title_raw"],
        chair_title=header["chair_title"],
        chair=header["chair"],
        executives=header["executives"],
        clerks=header["clerks"],
        attendance=header["attendance"],
        agenda=header["agenda"],
        utterances=utterances,
    )
