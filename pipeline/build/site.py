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

    def voices_url(self, unid: str, huid: int | None = None) -> str:
        f = self.fino.get(unid)
        if f is None:
            return ""
        url = VOICES_BODY.format(fino=f)
        return f"{url}#HUID{huid}" if huid else url


# ---------------------------------------------------------------- テンプレート

def page(site: Site, *, title: str, body: str, depth: int, description: str = "") -> str:
    root = "../" * depth or "./"
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
<link rel="stylesheet" href="{root}assets/style.css">
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
  <b>{FOOTER_HEAD}</b>
  {FOOTER_BODY}
  <span class="oss">{FOOTER_OSS}</span>
  <nav>
    <a href="{root}">トップ</a>
    <a href="{root}meetings/">会議一覧</a>
    <a href="{root}about/">このサイトについて</a>
    <a href="http://iasb-sv.town.onga.lg.jp/voices/index.asp">遠賀町議会 会議録検索システム</a>
  </nav>
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

def render_thread(site: Site, thread: dict) -> str:
    tr = site.transcripts[thread["unid"]]
    by_seq = {u.seq: u for u in tr.utterances}
    depth = 2

    topics = ""
    if thread["topics"]:
        items = "".join(f"<p>{t['no']}. {esc(t['title'])}</p>" for t in thread["topics"])
        topics = (
            '<h2>通告された質問事項</h2>'
            f'<div class="origin"><span class="tag">一般質問通告書より</span>{items}</div>'
        )

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
    title = thread["topics"][0]["title"] if thread["topics"] else f'{thread["questioner"]}議員の一般質問'
    body = (
        crumb(depth, (meeting["name"], f'meetings/{thread["meeting_id"]}/'), ("一般質問", ""))
        + f"<h1>{esc(title)}</h1>"
        + f'<p class="meta">{esc(thread["on"])} ／ {esc(thread["questioner_title"])}'
        f'（{esc(thread["questioner"])}） ／ 通告順{thread["order"]}</p>'
        + topics
        + "<h2>やりとり</h2>"
        + f'<div class="turns">{"".join(turns)}</div>'
        + (f'<a class="source" href="{site.voices_url(thread["unid"])}">この日の会議録をすべて読む</a>'
           if site.voices_url(thread["unid"]) else "")
    )
    desc = f'{thread["on"]} {thread["questioner"]}議員の一般質問。{title}'
    return page(site, title=title, body=body, depth=depth, description=desc)


def render_bill(site: Site, bill: dict) -> str:
    depth = 2
    meeting = site.meetings.get(bill["meeting_id"], {"name": bill["meeting"]})

    rows = [("提出された日", bill["submitted_on"])]
    if bill["decided_on"]:
        rows.append(("議決年月日", bill["decided_on"]))
    rows.append(("議決結果", bill["result"] or "（会議録から判定できませんでした）"))
    kv = "".join(f"<dt>{esc(k)}</dt><dd>{esc(str(v))}</dd>" for k, v in rows)

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
        f'<span class="t">{esc(t["topics"][0]["title"] if t["topics"] else t["questioner"] + "議員の一般質問")}</span>'
        f'<span class="s"><span class="mark">◆</span>{esc(t["questioner"])} 議員 ／ {esc(t["on"])}'
        + (f' ／ 質問事項 {len(t["topics"])}件' if t["topics"] else "")
        + "</span></a>"
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


def render_index(site: Site) -> str:
    latest = max(site.meetings.items(), key=lambda kv: kv[1]["first"], default=None)
    recent = sorted(site.threads, key=lambda t: t["on"], reverse=True)[:6]
    q_cards = "".join(
        f'<a class="card" href="questions/{thread_slug(t)}/">'
        f'<span class="t">{esc(t["topics"][0]["title"] if t["topics"] else t["questioner"] + "議員の一般質問")}</span>'
        f'<span class="s"><span class="mark">◆</span>{esc(t["questioner"])} 議員 ／ {esc(t["on"])}</span></a>'
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
        f'<p class="lead">平成17年からの会議録を、読みやすく並べ直しています。'
        f"いまは令和元年からの{len(site.meetings)}会議を載せています。</p>"
        '<h2>さがす</h2><div class="chips">'
        '<a class="chip" href="meetings/">定例会・臨時会から</a>'
        '<a class="chip" href="about/">このサイトについて</a>'
        "</div>"
        + latest_card
        + (f'<h2>最近の一般質問</h2><div class="stack">{q_cards}</div>' if q_cards else "")
    )
    return page(site, title="トップ", body=body, depth=0,
                description="遠賀町議会の会議録を、議事の流れと誰が何を言ったかが分かる形で並べ直した個人運営の非公式サイトです。")


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


# ---------------------------------------------------------------- 生成

def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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
    return Site(out=out, transcripts=transcripts, fino=fino, threads=threads,
                bills=bills, speakers=speakers, meetings=meetings)


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

    pages = sum(1 for _ in out.rglob("index.html"))
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"会議 {len(site.meetings)} / 一般質問 {len(site.threads)} / 議案 {len(site.bills)}")
    print(f"→ {out.relative_to(root)} に {pages}ページ（{size / 1_048_576:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
