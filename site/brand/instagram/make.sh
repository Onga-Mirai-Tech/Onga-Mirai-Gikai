#!/bin/sh
# Instagram 投稿用の画像（1080×1350 を4枚）を作り直す。macOS の Google Chrome を使う。
# 3枚目の画面は公開中のサイトを読み込んで表示する（ネット接続が要る）。
set -eu
cd "$(dirname "$0")"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
run() { "$CHROME" --headless=new --disable-gpu --hide-scrollbars "$@" >/dev/null 2>&1; }
for n in 1 2 3 4; do
  run --force-device-scale-factor=1 --virtual-time-budget=8000 --window-size=1080,1350 --screenshot="$PWD/post-$n.png" "file://$PWD/slides.html#s$n"
done
ls -l post-*.png
