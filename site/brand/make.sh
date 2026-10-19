#!/bin/sh
# ファビコン（PNG版）とSNS共有用の画像を作り直す。macOS の Google Chrome を使う。
# できた画像は site/assets/ に置き、そのままコミットする（ビルドでは作らない）。
set -eu
cd "$(dirname "$0")"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
shot() {
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
    --window-size="$2" --screenshot="$PWD/../assets/$3" "file://$PWD/$1" >/dev/null 2>&1
}
shot og.html 1200,630 og.png
shot icon.html 180,180 apple-touch-icon.png
ls -l ../assets/og.png ../assets/apple-touch-icon.png
