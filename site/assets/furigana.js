/* ふりがなの表示・非表示（2026-11-19）。
 *
 * ふりがなはビルドのときに付けてあり、ふだんは CSS で隠している。
 * このファイルは <html> に furigana という印を付け外しするだけ。
 *
 * 表示する設定にしたときだけ、その設定をこの端末のブラウザ（localStorage）に保存する。
 * どこにも送らない。表示しない設定に戻したら消す（プライバシーポリシー第1項）。
 *
 * <head> で読み込む。本文を描く前に印を付けて、ふりがなが後から出てくるちらつきを防ぐ。
 */
(function () {
  "use strict";
  var KEY = "furigana";
  var root = document.documentElement;
  var on = false;
  try { on = window.localStorage.getItem(KEY) === "on"; } catch (e) { /* 保存できない設定のブラウザ */ }
  if (on) root.classList.add("furigana");

  function save() {
    try {
      if (on) window.localStorage.setItem(KEY, "on");
      else window.localStorage.removeItem(KEY);
    } catch (e) { /* 保存できなくても、このページの表示は切り替わる */ }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var button = document.getElementById("furigana-toggle");
    if (!button) return;
    button.hidden = false;
    button.setAttribute("aria-pressed", on ? "true" : "false");
    button.addEventListener("click", function () {
      on = !on;
      root.classList.toggle("furigana", on);
      button.setAttribute("aria-pressed", on ? "true" : "false");
      save();
    });
  });
})();
