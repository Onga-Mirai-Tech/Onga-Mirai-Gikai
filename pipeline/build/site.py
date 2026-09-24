"""静的サイトを生成する。

P3（閲覧MVP）は**原文のみ**。AI要約はP4で足す。

- ページはビルド時に事前生成する。各ページに固有URLとOGPを持たせるため
- 外部からは何も読み込まない。フォントも端末が持っているものだけ
- JSはUI補助と検索にだけ使う（P5）
- スマホでの閲覧を最優先にする

出力先は `dist/`（git管理外）。

    python -m pipeline.build.site                令和元年5月以降を生成
    python -m pipeline.build.site --all          平成17年から全部
    python -m pipeline.build.site --limit-meetings 3   動作確認用
"""

from __future__ import annotations

import argparse
import collections
import functools
import hashlib
import html
import json
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pipeline.common.http import decode_cp932
from pipeline.fetch.voices import unid_to_date
from pipeline.parse import transcript as T

# スタイルシートの実体。中身の印をURLに付けて、古いCSSが残らないようにする。
CSS_SOURCE = Path(__file__).resolve().parents[2] / "site" / "assets" / "style.css"

SITE_NAME = "みらい議会"
SITE_SUB = "遠賀町版"
# 公開は令和元年〜を先行する（docs/設計ドラフト.md「2. 決定事項」）
DEFAULT_FROM = date(2019, 5, 1)

VOICES_BODY = (
    "http://iasb-sv.town.onga.lg.jp/voices/cgi/voiweb.exe"
    "?ACT=203&FINO={fino}&HATSUGENMODE=1&HYOUJIMODE=0&STYLE=0"
)

# ロゴ（C案「川と芽」）。外部読み込みをしないのでSVGを直接埋める。
LOGO_SVG = (
    '<svg width="30" height="30" viewBox="0 0 40 40" aria-hidden="true">'
    '<path d="M4 30 C 12 30, 14 24, 22 24 S 32 27, 36 25" fill="none" stroke="currentColor" '
    'stroke-width="2.4" stroke-linecap="round" opacity=".4"/>'
    '<path d="M4 35 C 12 35, 14 29, 22 29 S 32 32, 36 30" fill="none" stroke="currentColor" '
    'stroke-width="2.4" stroke-linecap="round" opacity=".25"/>'
    '<path d="M20 22 V 11" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>'
    '<path d="M20 15 C 20 10, 24 7, 28 7 C 28 12, 24 15, 20 15 Z" fill="currentColor"/>'
    '<path d="M20 19 C 20 15.5, 17 13.5, 14 13.5 C 14 17, 17 19, 20 19 Z" fill="currentColor" opacity=".55"/>'
    "</svg>"
)

FOOTER_HEAD = "遠賀町・遠賀町議会が公式に提供しているWEBサイトではありません"
FOOTER_BODY = (
    "遠賀町公式HPに公開されている情報を基にしたAI要約を含みます。"
    "正確な内容は必ず原典（遠賀町議会 会議録検索システム）をご確認ください。"
)
FOOTER_OSS = "なお、本WEBサイトはチームみらいが公開しているOSSを参考に作成されています。"

_BILL_NO_RE = re.compile(r"^(.+?)第(\d+)号$")
_KIND_SLUG = {
    "議案": "b", "発議": "h", "発委": "hi", "報告": "r", "意見書案": "i",
    "決議案": "k", "請願": "s", "陳情": "c", "選挙": "e", "推薦": "su", "指定": "si",
    "承認": "sn", "諮問": "sm",
}


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def paragraphs(text: str) -> str:
    """原文を段落に割る。会議録は改行＋全角スペースで段落が始まる。"""
    out = []
    for block in (text or "").split("\n"):
        block = block.strip("　 ")
        if block:
            out.append(f"<p>{esc(block)}</p>")
    return "".join(out) or "<p></p>"


def bill_slug(bill_id: str) -> str:
    """「2026-t3-b議案第42号」→「2026-t3-b41」相当のURL用の名前。"""
    meeting, _, number = bill_id.partition("-b")
    m = _BILL_NO_RE.match(number)
    if not m:
        return re.sub(r"[^A-Za-z0-9\-]", "-", bill_id)
    kind, no = m.groups()
    return f"{meeting}-{_KIND_SLUG.get(kind, 'x')}{no}"


def thread_slug(thread: dict) -> str:
    """「2026-t3-q01-K_R08060900031」→「2026-t3-q01」。同じ会議で日をまたぐ場合に備えて日付を足す。"""
    return f"{thread['meeting_id']}-{thread['on'].replace('-', '')}-q{thread['order']:02d}"


@dataclass
class Site:
    out: Path
    transcripts: dict[str, T.Transcript]
    fino: dict[str, int]
    threads: list[dict]
    bills: list[dict]
    speakers: dict[str, dict]
    meetings: dict[str, dict]
    slugs: dict[str, str]
    # 氏名 → その人が発言した議案（議案での発言の索引）
    member_bills: dict[str, list[dict]]
    # いちばん新しい会議の出欠表に載っている議員＝現職
    current_members: set[str]
    # 議案ID・一般質問ID → AI要約。検証を通ったものだけ入れる
    summaries: dict[str, dict]
    # 議案ID → 町HP「審議案件・結果」PDFの議決結果。表記が違うときに併記する
    kekka: dict[str, dict]

    def voices_url(self, unid: str, huid: int | None = None) -> str:
        f = self.fino.get(unid)
        if f is None:
            return ""
        url = VOICES_BODY.format(fino=f)
        return f"{url}#HUID{huid}" if huid else url


# ---------------------------------------------------------------- テンプレート

@functools.lru_cache(maxsize=1)
def css_version(path: str) -> str:
    """スタイルシートの中身から短い印を作り、URLに付ける。

    静的サイトなので、見た目を直しても**閲覧者のブラウザが古いCSSを持ち続ける**。
    中身が変わったときだけURLが変わるようにして、確実に読み直させる。
    中身が同じならURLも同じなので、無駄な再取得は起きない。
    """
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:8]


def page(site: Site, *, title: str, body: str, depth: int, description: str = "") -> str:
    root = "../" * depth or "./"
    v = css_version(str(CSS_SOURCE))
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}｜{SITE_NAME} -{SITE_SUB}-</title>
<meta name="description" content="{esc(description)}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:site_name" content="{SITE_NAME} -{SITE_SUB}-">
<meta property="og:type" content="article">
<link rel="stylesheet" href="{root}assets/style.css?v={v}">
</head>
<body>
<header class="site-head"><div class="inner">
  <a class="logo" href="{root}">{LOGO_SVG}<span><span class="n">{SITE_NAME}</span><span class="s">{SITE_SUB}</span></span></a>
  <span class="badge-unofficial">非公式</span>
</div></header>
<main>
{body}
</main>
<footer class="site-foot"><div class="inner">
  <p class="notice"><b>{FOOTER_HEAD}</b>{FOOTER_BODY}</p>
  <nav class="foot-nav" aria-label="サイト内の案内">
    <a href="{root}">トップ</a>
    <a href="{root}meetings/">会議一覧</a>
    <a href="{root}members/">議員の発言</a>
    <a href="{root}about/">このサイトについて</a>
  </nav>
  <p class="foot-source">出典：<a href="http://iasb-sv.town.onga.lg.jp/voices/index.asp">遠賀町議会 会議録検索システム</a><span class="ext">（外部サイト）</span></p>
  <p class="oss">{FOOTER_OSS}</p>
</div></footer>
</body>
</html>
"""


def crumb(depth: int, *parts: tuple[str, str]) -> str:
    root = "../" * depth or "./"
    items = [f'<a href="{root}">トップ</a>']
    for label, href in parts:
        items.append(f'<a href="{root}{href}">{esc(label)}</a>' if href else esc(label))
    return f'<p class="crumb">{" ＞ ".join(items)}</p>'


# ---------------------------------------------------------------- ページ

AI_NOTE = (
    "この要約は<b>AIが会議録から作ったもの</b>です。"
    "要点ごとに原文へのリンクを付けています。正確な内容は原文でご確認ください。"
)


def ai_sources(site: Site, huids: list, unid_of) -> str:
    """要点の根拠になった発言へのリンクを並べる。

    AI要約には必ず原文へのリンクを併記する（CLAUDE.md）。

    リンクの文字は「原文」ではなく**発言者の名前**にする。
    同じ要点に複数の根拠が付くのが普通で、「原文 原文 原文」と並ぶと
    どれを開けばよいか分からなくなるため。記号は会議録の話者記号に合わせる。
    """
    out = []
    for h in huids:
        unid = unid_of(h)
        tr = site.transcripts.get(unid)
        u = next((x for x in tr.utterances if x.huid == int(h)), None) if tr else None
        mark = {"質問者": "◆", "答弁者": "◎", "議長": "○"}.get(u.kind, "") if u else ""
        label = (u.name or u.title) if u else f"発言{h}"
        url = site.voices_url(unid, int(h))
        out.append(f'<a href="{url}"><span class="mark">{mark}</span>{esc(label)}</a>'
                   if url else f'<span>{esc(label)}</span>')
    return f'<p class="src"><span class="cap">原文</span>{"".join(out)}</p>' if out else ""


def summarized_topics(site: Site, thread: dict) -> set:
    """AI要約が載っている質問事項の番号。

    要約がない質問事項にリンクを張ると、押しても何も起きないページになる。
    検証を通らなかった要約は載せていないので、あるものだけを結ぶ。
    """
    s = site.summaries.get(thread["id"])
    if not s:
        return set()
    return {t.get("no") for t in s["summary"].get("topics", []) if t.get("points")}


def render_ai_thread(site: Site, thread: dict) -> str:
    s = site.summaries.get(thread["id"])
    if not s:
        return ""
    unid = thread["unid"]
    blocks = []
    for topic in s["summary"].get("topics", []):
        points = "".join(
            '<div class="qa">'
            f'<p class="q">{esc(pt.get("question", ""))}</p>'
            f'<p class="a">{esc(pt.get("answer", ""))}</p>'
            + ai_sources(site, pt.get("huids", []), lambda _h: unid)
            + "</div>"
            for pt in topic.get("points", [])
        )
        blocks.append(
            f'<h3 id="t{topic.get("no", "")}">{topic.get("no", "")}. '
            f'{esc(topic.get("title", ""))}</h3>{points}')
    if not blocks:
        return ""
    return (f'<h2>AI要約</h2><div class="ai"><p class="tag">{AI_NOTE}</p>'
            + "".join(blocks) + "</div>")


def render_ai_bill(site: Site, bill: dict) -> str:
    s = site.summaries.get(bill["id"])
    if not s:
        return ""
    # 発言IDから、その発言がどの日の会議録にあるかを引く
    where = {u["huid"]: u["unid"] for u in bill["utterances"]}
    blocks = []
    for pt in s["summary"].get("points", []):
        blocks.append(
            '<div class="qa">'
            f'<p class="k">{esc(pt.get("kind", ""))}</p>'
            f'<p>{esc(pt.get("text", ""))}</p>'
            + ai_sources(site, pt.get("huids", []), lambda h: where.get(int(h), ""))
            + "</div>"
        )
    if not blocks:
        return ""
    return (f'<h2>AI要約</h2><div class="ai"><p class="tag">{AI_NOTE}</p>'
            + "".join(blocks) + "</div>")


def thread_heading(thread: dict, *, with_name: bool = True) -> str:
    """一般質問の見出し。

    **質問事項が2件以上あるときは、1件目を見出しにしない。**
    1人が無関係な4テーマを質問することが普通にあり、1件目を見出しにすると
    そのページ全体がその話題であるかのように読める。
    """
    topics = thread.get("topics") or []
    if len(topics) == 1:
        return topics[0]["title"]
    return f'{thread["questioner"]}議員の一般質問' if with_name else "一般質問"


def topic_lines(thread: dict) -> str:
    """質問事項を通告書の番号つきで並べる。一覧のカードの中で使う。

    見出しだけでは何を尋ねたのかが分からないので、一覧の時点で中身を見せる。
    1件だけのときは見出しがその文言そのものなので、繰り返さない。
    """
    topics = thread.get("topics") or []
    if len(topics) < 2:
        return ""
    return '<span class="topics">' + "".join(
        f'<span><i>{t["no"]}</i>{esc(t["title"])}</span>' for t in topics
    ) + "</span>"


def render_thread(site: Site, thread: dict) -> str:
    tr = site.transcripts[thread["unid"]]
    by_seq = {u.seq: u for u in tr.utterances}
    depth = 2

    topics = ""
    if thread["topics"]:
        # 要約のある質問事項は、その箇所へ飛べるようにする。
        # 4テーマの往復が1ページに並ぶので、読みたいところまで遠い。
        linked = summarized_topics(site, thread)
        items = "".join(
            (f'<p><a href="#t{t["no"]}">{t["no"]}. {esc(t["title"])}</a></p>'
             if t["no"] in linked else f'<p>{t["no"]}. {esc(t["title"])}</p>')
            for t in thread["topics"]
        )
        note = ('<span class="tag">一般質問通告書より　／　項目を押すと要約の該当箇所に移動します</span>'
                if linked else '<span class="tag">一般質問通告書より</span>')
        topics = f'<h2>通告された質問事項</h2><div class="origin">{note}{items}</div>'

    turns = []
    for t in thread["turns"]:
        u = by_seq.get(t["seq"])
        if u is None or not u.text.strip():
            continue
        side = {"質問者": "q", "答弁者": "a"}.get(t["kind"], "c")
        who = f'{esc(t["title"])}（{esc(t["name"])}）' if t["name"] else ""
        mark = {"質問者": "◆", "答弁者": "◎", "議長": "○"}.get(t["kind"], "")
        link = site.voices_url(thread["unid"], u.huid)
        anchor = f' <a href="{link}" title="原文（会議録）で読む">原文</a>' if link else ""
        turns.append(
            f'<div class="turn {side}">'
            f'<div class="who"><span class="mark">{mark}</span>{who}{anchor}</div>'
            f'<div class="bubble">{paragraphs(u.text)}</div></div>'
        )

    meeting = site.meetings[thread["meeting_id"]]
    title = thread_heading(thread)
    body = (
        crumb(depth, (meeting["name"], f'meetings/{thread["meeting_id"]}/'), ("一般質問", ""))
        + f"<h1>{esc(title)}</h1>"
        + f'<p class="meta">{esc(thread["on"])} ／ {esc(thread["questioner_title"])}'
        f'（<a href="../../members/{member_slug(site, thread["questioner"])}/">'
        f'{esc(thread["questioner"])}</a>） ／ 通告順{thread["order"]}</p>'
        + topics
        + render_ai_thread(site, thread)
        + "<h2>やりとり</h2>"
        + f'<div class="turns">{"".join(turns)}</div>'
        + (f'<a class="source" href="{site.voices_url(thread["unid"])}">この日の会議録をすべて読む</a>'
           if site.voices_url(thread["unid"]) else "")
    )
    desc = f'{thread["on"]} {thread["questioner"]}議員の一般質問。{title}'
    return page(site, title=title, body=body, depth=depth, description=desc)


# 二つの公式資料で表記が違うときの断り。どちらかを正とは書かない。
ASIS_NOTE = (
    "遠賀町議会の会議録と、遠賀町ホームページの「審議案件・結果」とで表記が異なります。"
    "どちらが正しいかをこのサイトでは判断せず、<b>両方を原典のまま</b>載せています。"
)

MINUTES_LABEL = "会議録"
KEKKA_LABEL = "審議案件・結果"


def dual(minutes: str, pdf: str) -> tuple[str, bool]:
    """会議録の値とPDFの値を並べる。同じなら1つだけ返す。

    **どちらかに揃えない。** 「可決」と「認定」を同じ意味とみなして書き換えるのは
    こちらの判断が入る行為で、中立性の方針に反する（CLAUDE.md）。
    出どころを添えて両方そのまま出し、読む人が原典にあたれるようにする。
    """
    if not pdf or pdf == minutes:
        return esc(minutes), False
    pair = "".join(
        f'<span class="pair"><b>{esc(v)}</b><small>{esc(label)}</small></span>'
        for v, label in ((minutes, MINUTES_LABEL), (pdf, KEKKA_LABEL))
        if v
    )
    return pair, True


def render_bill(site: Site, bill: dict) -> str:
    depth = 2
    meeting = site.meetings.get(bill["meeting_id"], {"name": bill["meeting"]})
    k = site.kekka.get(bill["id"], {})

    rows = [("提出された日", esc(bill["submitted_on"]))]
    differs = False
    if bill["decided_on"] or k.get("decided_on"):
        html, d = dual(bill["decided_on"] or "", k.get("decided_on") or "")
        rows.append(("議決年月日", html))
        differs = differs or d
    result_html, d = dual(bill["result"] or "", k.get("result") or "")
    differs = differs or d
    rows.append(("議決結果", result_html or "（会議録から判定できませんでした）"))
    kv = "".join(f"<dt>{esc(key)}</dt><dd>{val}</dd>" for key, val in rows)
    if differs:
        kv += f'<dt></dt><dd><span class="asis">{ASIS_NOTE}</span></dd>'

    vote = ""
    if bill["vote"]:
        v = bill["vote"]
        if v["method"] == "起立" and v["yes"] is not None:
            vote = (
                '<h2>採決</h2><div class="tally">'
                f'<div class="row"><span>賛成</span><span>{v["yes"]}</span></div>'
                f'<div class="row"><span>反対</span><span>{v["no"]}</span></div>'
                f'<div class="row"><span>出席議員</span><span>{v["present"]}</span></div>'
                '<p class="cap">起立採決。会議録に記載があるのは人数までで、'
                'どの議員が賛成・反対したかは公表されていません。このサイトでは推測しません。</p>'
                "</div>"
            )
        else:
            vote = (
                '<h2>採決</h2><div class="referral">'
                f'{esc(v["method"])}による採決です。'
                "会議録に個人別の賛否の記載はありません。</div>"
            )

    if bill["referral_status"] == "committee":
        names = "・".join(bill["committees"])
        ref = (f'<div class="referral">この議案は{"所管ごとに" if len(bill["committees"]) > 1 else ""}'
               f"<b>{esc(names)}</b>に付託されました。委員会の会議録はHPに公開されていません。</div>")
    elif bill["referral_status"] == "omitted":
        ref = '<div class="referral">この議案は<b>委員会付託を省略</b>し、本会議で採決されました。</div>'
    else:
        ref = ""

    talk = []
    for u_ref in bill["utterances"]:
        tr = site.transcripts.get(u_ref["unid"])
        if tr is None:
            continue
        u = next((x for x in tr.utterances if x.seq == u_ref["seq"]), None)
        if u is None or u.kind not in ("質問者", "答弁者") or not u.text.strip():
            continue
        side = "q" if u.kind == "質問者" else "a"
        mark = "◆" if u.kind == "質問者" else "◎"
        link = site.voices_url(u_ref["unid"], u.huid)
        anchor = f' <a href="{link}" title="原文（会議録）で読む">原文</a>' if link else ""
        talk.append(
            f'<div class="turn {side}"><div class="who"><span class="mark">{mark}</span>'
            f'{esc(u.title)}（{esc(u.name)}）{anchor}</div>'
            f'<div class="bubble">{paragraphs(u.text)}</div></div>'
        )

    if talk:
        discussion = f'<h2>質疑・討論</h2><div class="turns">{"".join(talk)}</div>'
    else:
        discussion = '<h2>質疑・討論</h2><div class="referral">質疑・討論はありませんでした。</div>'

    body = (
        crumb(depth, (meeting["name"], f'meetings/{bill["meeting_id"]}/'), ("議案", ""))
        + f'<h1>{esc(bill["number"])}</h1>'
        + f'<p class="meta">{esc(bill["title"])}</p>'
        + f"<dl class=\"kv\">{kv}</dl>"
        + vote
        + render_ai_bill(site, bill)
        + discussion
        + (f"<h2>委員会付託</h2>{ref}" if ref else "")
    )
    return page(site, title=f'{bill["number"]} {bill["title"][:30]}', body=body, depth=depth,
                description=f'{bill["meeting"]} {bill["number"]} {bill["title"]}')


def render_meeting(site: Site, mid: str, meeting: dict) -> str:
    depth = 2
    threads = [t for t in site.threads if t["meeting_id"] == mid]
    bills = [b for b in site.bills if b["meeting_id"] == mid]

    days = "".join(
        f'<div class="card"><span class="t">{esc(d["on"])}（{d["issue"]}号）</span>'
        f'<span class="s">発言 {d["count"]}件'
        + (f' ／ <a href="{d["url"]}">会議録の原文</a>' if d["url"] else "")
        + "</span></div>"
        for d in meeting["days"]
    )

    q_cards = "".join(
        f'<a class="card" href="../../questions/{thread_slug(t)}/">'
        f'<span class="t">{esc(thread_heading(t))}</span>'
        f'<span class="s"><span class="mark">◆</span>{esc(t["questioner"])} 議員 ／ {esc(t["on"])}'
        + (f' ／ 質問事項 {len(t["topics"])}件' if t["topics"] else "")
        + "</span>" + topic_lines(t) + "</a>"
        for t in threads
    )

    b_cards = "".join(
        f'<a class="card" href="../../bills/{bill_slug(b["id"])}/">'
        f'<span class="t">{esc(b["number"])}　{esc(b["title"])}</span>'
        f'<span class="s">{esc(b["decided_on"] or b["submitted_on"])}'
        + (f' ／ {esc(b["result"])}' if b["result"] else "")
        + (f' ／ {esc("・".join(b["committees"]))}に付託' if b["committees"] else "")
        + "</span></a>"
        for b in bills
    )

    results = collections.Counter(b["result"] for b in bills if b["result"])
    summary = "、".join(f"{k}{v}件" for k, v in results.most_common())
    body = (
        crumb(depth, ("会議一覧", "meetings/"))
        + f'<h1>{esc(meeting["name"])}</h1>'
        + f'<p class="meta">{esc(meeting["first"])} 〜 {esc(meeting["last"])} ／ 全{len(meeting["days"])}日'
        f'<br>一般質問 {len(threads)}件 ／ 議案 {len(bills)}件'
        + (f"（{esc(summary)}）" if summary else "") + "</p>"
        + (f'<h2>一般質問</h2><div class="stack">{q_cards}</div>' if q_cards else "")
        + (f'<h2>議案</h2><div class="stack">{b_cards}</div>' if b_cards else "")
        + f'<h2>会議の日</h2><div class="stack">{days}</div>'
    )
    return page(site, title=meeting["name"], body=body, depth=depth,
                description=f'{meeting["name"]}の一般質問{len(threads)}件と議案{len(bills)}件。')


def render_meeting_index(site: Site) -> str:
    depth = 1
    by_year = collections.defaultdict(list)
    for mid, m in site.meetings.items():
        by_year[m["first"][:4]].append((mid, m))
    out = []
    for year in sorted(by_year, reverse=True):
        cards = "".join(
            f'<a class="card" href="../meetings/{mid}/"><span class="t">{esc(m["name"])}</span>'
            f'<span class="s">{esc(m["first"])} 〜 {esc(m["last"])} ／ 全{len(m["days"])}日</span></a>'
            for mid, m in sorted(by_year[year], key=lambda x: x[1]["first"], reverse=True)
        )
        out.append(f'<h2>{year}年</h2><div class="stack">{cards}</div>')
    body = crumb(depth) + "<h1>会議一覧</h1>" + "".join(out)
    return page(site, title="会議一覧", body=body, depth=depth,
                description="遠賀町議会の定例会・臨時会の一覧。")


# 「令和元年」の「元」は数字ではない。会議録がそう書いているので、そのまま受ける。
_ERA_HEAD_RE = re.compile(r"^(令和|平成|昭和)(元|\d+)年")


def coverage_label(site: Site) -> str:
    """載せている期間の言い方を、いちばん古い会議の名前から作る。

    「令和元年第5回定例会」→「令和元年」。和暦をこちらで組み立てず、
    会議録に書かれている表記をそのまま使う。
    """
    if not site.meetings:
        return ""
    oldest = min(site.meetings.values(), key=lambda m: m["first"])
    m = _ERA_HEAD_RE.match(oldest["name"])
    if not m:
        return oldest["first"][:4] + "年"
    era, year = m.groups()
    return f"{era}{year}年"


def render_index(site: Site) -> str:
    latest = max(site.meetings.items(), key=lambda kv: kv[1]["first"], default=None)
    recent = sorted(site.threads, key=lambda t: t["on"], reverse=True)[:6]
    q_cards = "".join(
        f'<a class="card" href="questions/{thread_slug(t)}/">'
        f'<span class="t">{esc(thread_heading(t))}</span>'
        f'<span class="s"><span class="mark">◆</span>{esc(t["questioner"])} 議員 ／ {esc(t["on"])}</span>'
        + topic_lines(t) + "</a>"
        for t in recent
    )
    latest_card = ""
    if latest:
        mid, m = latest
        threads = sum(1 for t in site.threads if t["meeting_id"] == mid)
        bills = sum(1 for b in site.bills if b["meeting_id"] == mid)
        latest_card = (
            f'<h2>いちばん新しい会議</h2><a class="card" href="meetings/{mid}/">'
            f'<span class="t">{esc(m["name"])}</span>'
            f'<span class="s">{esc(m["first"])} 〜 {esc(m["last"])}・全{len(m["days"])}日<br>'
            f"一般質問 {threads}件 ／ 議案 {bills}件</span></a>"
        )
    body = (
        "<h1>遠賀町議会で、<br>いま話されていること</h1>"
        f'<p class="lead">{esc(coverage_label(site))}からの会議録を、読みやすく並べ直しています。'
        f"いま載せているのは{len(site.meetings)}会議です。"
        "それより前の会議録は、遠賀町議会の会議録検索システムでご覧になれます。</p>"
        '<h2>さがす</h2><div class="chips">'
        '<a class="chip" href="meetings/">定例会・臨時会</a>'
        '<a class="chip" href="members/">議員の発言</a>'
        '<a class="chip" href="about/">このサイトについて</a>'
        "</div>"
        + latest_card
        + (f'<h2>最近の一般質問</h2><div class="stack">{q_cards}</div>' if q_cards else "")
    )
    return page(site, title="トップ", body=body, depth=0,
                description="遠賀町議会の会議録を、議事の流れと誰が何を言ったかが分かる形で並べ直した個人運営の非公式サイトです。")


def ai_model(site: Site) -> str:
    """要約に使ったモデル名。要約JSONに記録してあるものを読む。

    こちらで書き足すと、実際に使ったモデルとずれる。
    """
    names = sorted({s.get("model", "") for s in site.summaries.values()} - {""})
    return "・".join(names) or "（まだありません）"


def ai_prompt_version(site: Site) -> str:
    vs = sorted({s.get("prompt_version", "") for s in site.summaries.values()} - {""})
    return "・".join(vs) or "（まだありません）"


def render_about(site: Site) -> str:
    depth = 1
    body = (
        crumb(depth)
        + "<h1>このサイトについて</h1>"
        + '<p class="lead">遠賀町議会の会議録をもとに、議事の流れと「誰が何を言い、町がどう答えたか」を'
        "わかりやすく見せる、<b>個人が運営する非公式サイト</b>です。</p>"
        "<h2>立場</h2><div class=\"referral\">"
        "遠賀町・遠賀町議会が公式に提供しているWEBサイトではありません。"
        "運営しているのは個人で、遠賀町・遠賀町議会とは関係がありません。</div>"
        "<h2>守っていること</h2><div class=\"stack\">"
        '<div class="card"><span class="t">評価や順位づけをしません</span>'
        '<span class="s">議員・会派・町に対する評価、点数付け、ランキング、賛否の色分けはしません。'
        "並べ順は日付・議案番号・通告順といった機械的なものだけです。</span></div>"
        '<div class="card"><span class="t">個人別の賛否は扱いません</span>'
        '<span class="s">どの議員が賛成・反対したかは公表されていません。'
        "会議録に記載があるのは起立採決の人数までで、このサイトでは推測しません。</span></div>"
        '<div class="card"><span class="t">原文へのリンクを必ず付けます</span>'
        '<span class="s">各発言から、遠賀町議会 会議録検索システムの該当箇所へ移動できます。'
        "正確な内容は必ず原典をご確認ください。</span></div>"
        '<div class="card"><span class="t">委員会の会議録はありません</span>'
        '<span class="s">議案が常任委員会に付託された場合、委員会の会議録はHPに公開されていません。'
        "付託されたという事実と委員会名だけを表示します。</span></div>"
        "</div>"
        "<h2>AI要約について</h2><div class=\"stack\">"
        '<div class="card"><span class="t">AIが書いた部分は点線の枠で囲みます</span>'
        '<span class="s">一般質問は通告書の質問事項ごとに、議案は質疑・討論があったものだけを要約しています。'
        "会議全体や定例会全体のまとめにはAIを使いません。要約の要約は原文から遠くなり、誤りが増えるためです。</span></div>"
        '<div class="card"><span class="t">要点ごとに原文へのリンクを付けます</span>'
        '<span class="s">どの発言をもとにした要点なのかが分かるようにしています。'
        "リンクの文字は発言した人の名前です。</span></div>"
        '<div class="card"><span class="t">機械で確かめてから載せています</span>'
        '<span class="s">要約に出てくる数字が原文にあるか、引用した発言が実在するかを、'
        "公開前に1件ずつ突き合わせています。合わないものは人が確認するまで載せません。</span></div>"
        '<div class="card"><span class="t">それでも誤りは残ります</span>'
        '<span class="s">AIの要約は完全ではありません。おかしいと思われた箇所は、'
        "必ず原文をご確認ください。お気づきの点はご連絡いただけると助かります。</span></div>"
        "</div>"
        + f'<div class="referral">使用しているモデルは <b>{esc(ai_model(site))}</b>、'
        f'要約の指示の版は <b>{esc(ai_prompt_version(site))}</b> です。'
        f'いま載せている要約は{len(site.summaries)}件です。</div>'
        + "<h2>載せている期間</h2><div class=\"referral\">"
        f"このサイトが載せているのは<b>{esc(coverage_label(site))}以降</b>の会議録です。"
        "それより前の会議録は、下記の遠賀町議会 会議録検索システムでご覧になれます。</div>"
        "<h2>データの出典</h2><div class=\"stack\">"
        '<a class="card" href="http://iasb-sv.town.onga.lg.jp/voices/index.asp">'
        '<span class="t">遠賀町議会 会議録検索システム（VOICES）</span>'
        '<span class="s">会議録の原文</span></a>'
        '<a class="card" href="https://www.town.onga.lg.jp/site/gikai/list20-62.html">'
        '<span class="t">一般質問通告書（遠賀町公式ホームページ）</span>'
        '<span class="s">質問事項・質問の要旨・質問の相手</span></a>'
        "</div>"
    )
    return page(site, title="このサイトについて", body=body, depth=depth,
                description="個人運営の非公式サイトであること、守っているルール、データの出典。")


_MEMBER_TITLE_RE = re.compile(r"^[０-９0-9]+番議員$")


def is_member_role(title: str) -> bool:
    """その発言が**議員としての発言**か。

    議員から町長になる人がいる（古野修）。町長・執行部としての発言まで議員ページに
    並べると、答弁が膨大で索引として使えなくなる。議員時代の発言だけを載せる。
    議長・副議長・委員長は議員が務めるので含める。
    """
    return bool(
        _MEMBER_TITLE_RE.match(title)
        or title in ("議長", "副議長", "臨時議長")
        or "委員長" in title
    )


def member_slug(site: Site, name: str) -> str:
    """議員ページのURLに使う名前。

    `masters/speakers.json` の `slugs` にローマ字があればそれを使う。
    無ければ氏名をそのまま使う（URLとしては動くが、共有するとパーセント符号化されて
    読みにくくなる。氏名の読みはこちらでは決められないので、人が入れる）。
    """
    return site.slugs.get(name, name)


def members_in_scope(site: Site) -> list[dict]:
    """議席を持ったことがある人。町長になった元議員も含む。"""
    return sorted(
        (s for s in site.speakers.values() if s["seats"]),
        key=lambda s: (s["last"], s["name"]), reverse=True,
    )


def render_member_index(site: Site) -> str:
    depth = 1
    members = members_in_scope(site)
    current = [m for m in members if m["name"] in site.current_members]
    past = [m for m in members if m["name"] not in site.current_members]

    def cards(group: list[dict]) -> str:
        return "".join(
            f'<a class="card" href="../members/{member_slug(site, m["name"])}/">'
            f'<span class="t">{esc(m["name"])}</span>'
            f'<span class="s">{esc("・".join(m["seats"]))}'
            f' ／ {esc(m["first"])} 〜 {esc(m["last"])}'
            + (f' ／ {esc(m["kind"])}' if m["kind"] != "議員" else "")
            + "</span></a>"
            for m in group
        )

    body = (
        crumb(depth)
        + "<h1>議員の発言</h1>"
        + '<p class="lead">このサイトが載せている会議録に発言のある議員です。'
        "現職かどうかは、いちばん新しい会議の出欠表に載っているかで分けています。"
        "並び順は最後に発言した日の新しい順で、順位づけではありません。</p>"
        + '<div class="referral">議員から町長になった人も載せていますが、'
        "<b>載せているのは議員だったときの発言だけ</b>です。"
        "町長・執行部としての答弁は量が膨大で、索引として使えなくなるため含めていません。</div>"
        + (f'<h2>現職議員</h2><div class="stack">{cards(current)}</div>' if current else "")
        + (f'<h2>過去の議員</h2><div class="stack">{cards(past)}</div>' if past else "")
    )
    return page(site, title="議員の発言", body=body, depth=depth,
                description="遠賀町議会の議員の一覧。発言した一般質問と議案への索引です。")


def render_member(site: Site, m: dict) -> str:
    depth = 2
    name = m["name"]
    threads = sorted((t for t in site.threads if t["questioner"] == name),
                     key=lambda t: t["on"], reverse=True)
    bills = site.member_bills.get(name, [])

    # 年別の件数。**その人のページの中だけ**で示し、議員間で並べた順位表は作らない。
    per_year = collections.Counter()
    for t in threads:
        per_year[t["on"][:4]] += len(t["topics"]) or 1
    counts = "".join(
        f'<div class="card"><span class="t">{esc(y)}年</span>'
        f'<span class="s">質問事項 {c}件</span></div>'
        for y, c in sorted(per_year.items(), reverse=True)[:6]
    )

    q_cards = "".join(
        f'<a class="card" href="../../questions/{thread_slug(t)}/">'
        f'<span class="t">{esc(thread_heading(t, with_name=False))}</span>'
        f'<span class="s">{esc(site.meetings[t["meeting_id"]]["name"])} ／ {esc(t["on"])}'
        + (f' ／ 質問事項 {len(t["topics"])}件' if len(t["topics"]) > 1 else "")
        + "</span>" + topic_lines(t) + "</a>"
        for t in threads
    )

    b_cards = "".join(
        f'<a class="card" href="../../bills/{bill_slug(b["id"])}/">'
        f'<span class="t">{esc(b["number"])}　{esc(b["title"])}</span>'
        f'<span class="s">{esc(b["meeting"])} ／ {esc(b["decided_on"] or b["submitted_on"])}</span></a>'
        for b in bills[:40]
    )

    roles = "".join(
        f'<div class="row"><span>{esc(r["title"])}</span>'
        f'<span>{esc(r["first"])} 〜 {esc(r["last"])}</span></div>'
        for r in sorted(m["roles"], key=lambda r: r["first"])
    )

    non_member = [r["title"] for r in m["roles"] if not is_member_role(r["title"])]
    note_index = (
        (f'<div class="referral">この方は{esc("・".join(sorted(set(non_member))))}'
         "も務めています。ここに載せているのは<b>議員だったときの発言だけ</b>です。</div>"
         if non_member else "")
        + '<div class="referral">このページは索引です。発言の内容は各ページでご覧ください。'
        "賛否の記録は公表されていないため、このサイトでは扱っていません。</div>"
    )
    note_counts = (
        '<div class="referral">この数は一般質問の質問事項の件数です。'
        "ほかの議員と並べた順位づけはしていません。"
        "回数の多い少ないは、議員の仕事ぶりを表すものではありません。</div>"
    )

    body = (
        crumb(depth, ("議員", "members/"))
        + f"<h1>{esc(name)}</h1>"
        + f'<p class="meta">{esc("・".join(m["seats"]))} ／ {esc(m["first"])} 〜 {esc(m["last"])}'
        + (f' ／ 現在は{esc(m["kind"])}' if m["kind"] != "議員" else "") + "</p>"
        + (f'<h2>役職の記録</h2><div class="tally">{roles}</div>' if roles else "")
        + (f'<h2>質問した回数</h2><div class="stack">{counts}</div>{note_counts}' if counts else "")
        + (f'<h2>一般質問</h2><div class="stack">{q_cards}</div>' if q_cards else "")
        + (f'<h2>議案での発言</h2><div class="stack">{b_cards}</div>' if b_cards else "")
        + note_index
    )
    return page(site, title=name, body=body, depth=depth,
                description=f'{name}議員の一般質問と議案での発言の索引。')


# ---------------------------------------------------------------- 生成

def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_summaries(root: Path) -> tuple[dict[str, dict], int]:
    """AI要約を読む。**検証を通らなかったものは載せない。**

    原文にない数字や実在しない発言IDを含む要約を出すと、
    中立性・正確性の約束（CLAUDE.md）を破ることになる。
    人が確認して直すまでは、そのページに要約を出さないだけにする。
    """
    out: dict[str, dict] = {}
    held = 0
    for sub in ("threads", "bills"):
        for f in sorted((root / "data" / "summaries" / sub).glob("*.json")):
            s = json.loads(f.read_text(encoding="utf-8"))
            if not s.get("verified"):
                held += 1
                continue
            out[s["id"]] = s
    return out, held


def load_kekka(root: Path) -> dict[str, dict]:
    """町HP「審議案件・結果」から読んだ議決結果（`pipeline.verify.results --emit`）。"""
    path = root / "data" / "verify" / "kekka.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_fino(root: Path) -> dict[str, int]:
    """state.json から UNID → FINO を作る。原文リンクの組み立てに使う。"""
    state_path = root / "state.json"
    if not state_path.exists():
        return {}
    out: dict[str, int] = {}
    for e in json.loads(state_path.read_text(encoding="utf-8")).get("fetched", []):
        m = re.search(r"data/raw/voices/(K_[A-Z0-9]+)\.html$", e["path"])
        f = re.search(r"FINO=(\d+)", e["url"])
        if m and f:
            out[m.group(1)] = int(f.group(1))
    return out


def build(root: Path, out: Path, since: date, limit_meetings: int | None) -> Site:
    from pipeline.parse.bills import meeting_id

    norm = root / "data" / "normalized"
    threads = json.loads((norm / "threads.json").read_text(encoding="utf-8"))
    bills = json.loads((norm / "bills.json").read_text(encoding="utf-8"))
    speakers = {s["name"]: s for s in json.loads((norm / "speakers.json").read_text(encoding="utf-8"))}

    transcripts: dict[str, T.Transcript] = {}
    meetings: dict[str, dict] = {}
    fino = load_fino(root)

    for f in sorted((root / "data" / "raw" / "voices").glob("K_*.html")):
        on = unid_to_date(f.stem)
        if on < since:
            continue
        tr = T.parse(decode_cp932(f.read_bytes()), f.stem)
        transcripts[f.stem] = tr
        mid, name = meeting_id(tr.title_raw)
        m = meetings.setdefault(mid, {"name": name, "days": [], "first": str(on), "last": str(on)})
        issue = int(re.sub(r"\D", "", f.stem[-5:-1]) or 0)
        m["days"].append({"on": str(on), "issue": issue, "count": len(tr.utterances),
                          "url": VOICES_BODY.format(fino=fino[f.stem]) if f.stem in fino else ""})
        m["first"] = min(m["first"], str(on))
        m["last"] = max(m["last"], str(on))

    if limit_meetings:
        keep = sorted(meetings, key=lambda k: meetings[k]["first"], reverse=True)[:limit_meetings]
        meetings = {k: meetings[k] for k in keep}
        transcripts = {k: v for k, v in transcripts.items()
                       if any(d["on"] in {x["on"] for x in meetings[mk]["days"]}
                              for mk in meetings for d in meetings[mk]["days"]
                              if d["on"] == str(unid_to_date(k)))}

    for m in meetings.values():
        m["days"].sort(key=lambda d: d["on"])

    threads = [t for t in threads if t["meeting_id"] in meetings and t["unid"] in transcripts]
    bills = [b for b in bills if b["meeting_id"] in meetings]

    # 議案での発言を、氏名から引けるようにする
    member_bills: dict[str, list[dict]] = collections.defaultdict(list)
    for b in bills:
        seen: set[str] = set()
        for ref in b["utterances"]:
            tr = transcripts.get(ref["unid"])
            if tr is None:
                continue
            u = next((x for x in tr.utterances if x.seq == ref["seq"]), None)
            if u is None or u.kind not in ("質問者", "答弁者") or not u.name:
                continue
            if not is_member_role(u.title):
                continue  # 町長・執行部としての答弁は議員ページに載せない
            if u.name not in seen:
                seen.add(u.name)
                member_bills[u.name].append(b)
    for v in member_bills.values():
        v.sort(key=lambda b: b["decided_on"] or b["submitted_on"], reverse=True)

    # 現職＝いちばん新しい会議の出欠表に載っている議員。日付の当て推量をしない。
    current_members: set[str] = set()
    if transcripts:
        newest = max(transcripts, key=lambda k: unid_to_date(k))
        aliases = {}
        mp = root / "masters" / "speakers.json"
        if mp.exists():
            aliases = json.loads(mp.read_text(encoding="utf-8")).get("aliases", {})
        current_members = {
            aliases.get(a.name, a.name) for a in transcripts[newest].attendance
            if a.status in ("出席", "欠席", "記載なし")
        }

    overrides = json.loads((root / "masters" / "speakers.json").read_text(encoding="utf-8")) \
        if (root / "masters" / "speakers.json").exists() else {}
    kekka = load_kekka(root)
    summaries, held = load_summaries(root)
    print(f"  AI要約 {len(summaries)}件を読みました"
          + (f"（検証が通らず保留 {held}件）" if held else ""))
    return Site(out=out, transcripts=transcripts, fino=fino, threads=threads,
                bills=bills, speakers=speakers, meetings=meetings,
                slugs=overrides.get("slugs", {}), member_bills=dict(member_bills),
                current_members=current_members, summaries=summaries, kekka=kekka)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="静的サイトを生成する")
    parser.add_argument("--all", action="store_true", help="平成17年から全部生成する")
    parser.add_argument("--limit-meetings", type=int, help="新しい順にこの会議数だけ生成する")
    parser.add_argument("--out", default="dist")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[2]
    out = root / args.out
    since = date(2005, 1, 1) if args.all else DEFAULT_FROM

    if not (root / "data" / "normalized" / "threads.json").exists():
        print("data/normalized が空です。先に pipeline.parse.* を実行してください。", file=sys.stderr)
        return 1

    site = build(root, out, since, args.limit_meetings)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(root / "site" / "assets", out / "assets")

    write(out / "index.html", render_index(site))
    write(out / "about" / "index.html", render_about(site))
    write(out / "meetings" / "index.html", render_meeting_index(site))
    for mid, m in site.meetings.items():
        write(out / "meetings" / mid / "index.html", render_meeting(site, mid, m))
    for t in site.threads:
        write(out / "questions" / thread_slug(t) / "index.html", render_thread(site, t))
    for b in site.bills:
        write(out / "bills" / bill_slug(b["id"]) / "index.html", render_bill(site, b))
    write(out / "members" / "index.html", render_member_index(site))
    for m in members_in_scope(site):
        write(out / "members" / member_slug(site, m["name"]) / "index.html", render_member(site, m))

    pages = sum(1 for _ in out.rglob("index.html"))
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"会議 {len(site.meetings)} / 一般質問 {len(site.threads)} / 議案 {len(site.bills)}"
          f" / 議員 {len(members_in_scope(site))}")
    print(f"→ {out.relative_to(root)} に {pages}ページ（{size / 1_048_576:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
