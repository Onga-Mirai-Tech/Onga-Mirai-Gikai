/* みらい議会＠遠賀町 — ことばで検索
 *
 * このサイトで唯一の JavaScript。検索ページでだけ読み込む。
 *
 * - 検索は**ブラウザの中だけ**で行う。索引（index.json）を自分のサーバーから
 *   1回取ってくるだけで、検索語はどこにも送らない。
 * - 検索語はURLの「#」より後ろに持たせる。この部分はサーバーに届かないので、
 *   アクセスログに検索語が残らない。それでいて検索結果のURLは人に送れる。
 * - 並び順は新しい順だけ。「一致の多い順」は関連度という順位付けに見えるので
 *   使わない（docs/設計ドラフト.md「中立性」）。
 * - 一致したのがAI要約だけのときは、その旨を表示する。AIの言い換えに一致した
 *   だけで、原文にその語があるとは限らないため。
 * - 結果はすべて textContent で組み立てる。索引の文字列をHTMLとして解釈しない。
 */
(function () {
  "use strict";

  var form = document.getElementById("search-form");
  var input = document.getElementById("q");
  var selWho = document.getElementById("w");
  var selYear = document.getElementById("y");
  var selKind = document.getElementById("k");
  var status = document.getElementById("search-status");
  var list = document.getElementById("search-results");
  var root = form.getAttribute("data-root") || "../";

  var entries = null;
  var SNIPPET = 38; // AI要約の抜粋で、一致した語の前後に見せる文字数

  // 全角・半角、大文字・小文字の違いを無視する。
  function norm(s) {
    return (s || "").normalize("NFKC").toLowerCase();
  }

  function readHash() {
    var p = {};
    location.hash.replace(/^#/, "").split("&").forEach(function (kv) {
      if (!kv) return;
      var i = kv.indexOf("=");
      var k = i < 0 ? kv : kv.slice(0, i);
      var v = i < 0 ? "" : kv.slice(i + 1);
      try { p[k] = decodeURIComponent(v.replace(/\+/g, " ")); } catch (e) { p[k] = v; }
    });
    return p;
  }

  function writeHash(p) {
    var parts = [];
    ["q", "w", "y", "k"].forEach(function (k) {
      if (p[k]) parts.push(k + "=" + encodeURIComponent(p[k]));
    });
    var h = parts.length ? "#" + parts.join("&") : "";
    // 検索のたびに履歴を積まない。戻るボタンで検索前のページに戻れるようにする。
    history.replaceState(null, "", location.pathname + location.search + h);
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // 正規化した文字列と、その各位置が元の文字列のどこに当たるかの対応表。
  // 正規化で長さが変わる文字がある（「㎡」→「m2」）。正規化後の位置でそのまま
  // 元の文字列を切ると、それより後ろの印が1文字ずつずれる（実測: 索引の3件）。
  function mapNorm(text) {
    var n = "";
    var idx = [];
    for (var i = 0; i < text.length; ) {
      var cp = text.codePointAt(i);
      var ch = String.fromCodePoint(cp);
      var nc = norm(ch);
      for (var j = 0; j < nc.length; j++) idx.push(i);
      n += nc;
      i += ch.length;
    }
    idx.push(text.length);
    return { n: n, idx: idx };
  }

  function snippet(text, word) {
    var m = mapNorm(text);
    var i = m.n.indexOf(word);
    if (i < 0) return "";
    var s = m.idx[i];
    var e = m.idx[i + word.length];
    var start = Math.max(0, s - SNIPPET);
    var end = Math.min(text.length, e + SNIPPET);
    return (start > 0 ? "…" : "") + text.slice(start, end) + (end < text.length ? "…" : "");
  }

  // 一致した語だけを <mark> で囲む。文字列はすべて textContent で入れる。
  function highlight(parent, text, word) {
    var m = mapNorm(text);
    var pos = 0;   // 元の文字列での位置
    var from = 0;  // 正規化後の文字列での探し始め
    var i;
    while (word && (i = m.n.indexOf(word, from)) >= 0) {
      var s = m.idx[i];
      var e = m.idx[i + word.length];
      if (s > pos) parent.appendChild(document.createTextNode(text.slice(pos, s)));
      parent.appendChild(el("mark", null, text.slice(s, e)));
      pos = e;
      from = i + word.length;
    }
    if (pos < text.length) parent.appendChild(document.createTextNode(text.slice(pos)));
  }

  function render(p) {
    var words = norm(p.q).split(/\s+/).filter(Boolean);
    list.textContent = "";

    if (!words.length && !p.w && !p.y && !p.k) {
      status.textContent = "ことばを入れるか、発言者・年・種類を選んでください。";
      return;
    }

    var hits = [];
    entries.forEach(function (e) {
      if (p.k && e.k !== p.k) return;
      if (p.y && e.y !== p.y) return;
      if (p.w && e.w.indexOf(p.w) < 0) return;
      // 複数の語はすべて含むものだけ（かつ）。公式の文言とAI要約のどちらかにあればよい。
      var inOfficial = true;
      var inAny = true;
      for (var i = 0; i < words.length; i++) {
        var inO = e.on.indexOf(words[i]) >= 0;
        var inS = e.sn.indexOf(words[i]) >= 0;
        if (!inO) inOfficial = false;
        if (!inO && !inS) { inAny = false; break; }
      }
      if (!inAny) return;
      hits.push({ e: e, aiOnly: words.length > 0 && !inOfficial });
    });

    // 新しい順。同じ日なら索引に並んだ順（通告順・議案番号順）を保つ。
    hits.sort(function (a, b) { return a.e.d < b.e.d ? 1 : a.e.d > b.e.d ? -1 : a.e.i - b.e.i; });

    status.textContent = hits.length
      ? hits.length + "件見つかりました（新しい順）"
      : "見つかりませんでした。ことばを短くするか、ほかの言い方でお試しください。";

    hits.forEach(function (h) {
      var e = h.e;
      var a = el("a", "card");
      a.href = root + e.u;
      // 見出しに一致した語も印をつける。どこに一致したのかが見えないと、
      // なぜこの結果が出たのか分からない。
      var title = el("span", "t");
      highlight(title, e.t, words[0] || "");
      a.appendChild(title);
      a.appendChild(el("span", "s", (e.k === "q" ? "一般質問" : "議案") + " ／ " + e.m + " ／ " + e.d));

      if (e.tp && e.tp.length > 1) {
        var tp = el("span", "topics");
        e.tp.forEach(function (x, n) {
          var line = el("span");
          line.appendChild(el("i", null, String(n + 1)));
          highlight(line, x, words[0] || "");
          tp.appendChild(line);
        });
        a.appendChild(tp);
      }

      if (h.aiOnly) {
        // どの語がAI要約だけに一致したかを見せる。原文にあるとは限らない。
        var w = words.filter(function (x) { return e.on.indexOf(x) < 0; })[0];
        var box = el("span", "ai-hit");
        box.appendChild(el("span", "ai-tag", "AI要約に一致"));
        var sn = el("span", "ai-snip");
        highlight(sn, snippet(e.s, w), w);
        box.appendChild(sn);
        a.appendChild(box);
      }
      list.appendChild(a);
    });
  }

  function current() {
    return { q: input.value.trim(), w: selWho.value, y: selYear.value, k: selKind.value };
  }

  function apply(p) {
    input.value = p.q || "";
    selWho.value = p.w || "";
    selYear.value = p.y || "";
    selKind.value = p.k || "";
  }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var p = current();
    writeHash(p);
    if (entries) render(p);
  });
  [selWho, selYear, selKind].forEach(function (s) {
    s.addEventListener("change", function () {
      var p = current();
      writeHash(p);
      if (entries) render(p);
    });
  });
  window.addEventListener("hashchange", function () {
    var p = readHash();
    apply(p);
    if (entries) render(p);
  });

  status.textContent = "索引を読み込んでいます…";
  fetch(root + "search/index.json", { credentials: "same-origin" })
    .then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    })
    .then(function (data) {
      // 照合用に正規化した文字列を先に作っておく。検索のたびに作り直さない。
      entries = data.map(function (e, i) {
        e.i = i;
        e.on = norm(e.o);
        e.sn = norm(e.s);
        return e;
      });
      var p = readHash();
      apply(p);
      render(p);
    })
    .catch(function () {
      status.textContent = "索引を読み込めませんでした。時間をおいて、ページを読み込み直してください。";
    });
})();
