#!/bin/sh
# 会議録を手元（日本）で取得して、GitHub の月次更新を起動する。
#
# 会議録検索システムは、2026年11月から海外からの接続を受け付けなくなった。
# GitHub Actions の実行環境はアメリカにあるため、会議録の取得だけをここで行う。
# 通告書・審議案件結果の取得、整形、AI要約、PRの作成は、これまでどおり GitHub で動く。
#
# 使い方（毎月2日以降の、日本時間 0〜6時に）:
#     scripts/fetch-voices-local.sh            取得して push し、月次更新を起動する
#     scripts/fetch-voices-local.sh --dry-run  何件取りに行くかだけ確かめる（通信しない）
#
# 取得のマナー（3秒間隔・逐次・深夜帯のみ）は pipeline.fetch.voices が守る。
# 深夜帯以外に実行すると、取得の段階で断られて何も起きない。
set -eu

cd "$(dirname "$0")/.."
ROOT=$(pwd)
PY="$ROOT/.venv/bin/python"
TODAY=$(date +%Y-%m-%d)

if [ "${1:-}" = "--dry-run" ]; then
  exec "$PY" -m pipeline.fetch.voices --dry-run
fi

echo "■ 最新にそろえる"
git pull -q --rebase
git -C data pull -q --rebase

echo "■ 会議録を取得する"
# 取得中に Mac がスリープしないようにする。
caffeinate -i "$PY" -m pipeline.fetch.voices

echo "■ 原文を data リポジトリに入れる"
if [ -n "$(git -C data status --porcelain raw)" ]; then
  git -C data add -A raw
  git -C data commit -q -m "会議録を取得（手元） $TODAY"
  git -C data push -q
  echo "  push しました"
else
  echo "  新しい原文はありません"
fi

echo "■ 取得の記録（state.json）を入れる"
if [ -n "$(git status --porcelain state.json)" ]; then
  git add state.json
  git commit -q -m "月次取得の記録を更新（手元） $TODAY"
  git push -q
  echo "  push しました"
else
  echo "  変更はありません"
fi

echo "■ GitHub の月次更新を起動する"
gh workflow run monthly-update.yml
echo "  起動しました。進み具合: https://github.com/Onga-Mirai-Tech/Onga-Mirai-Gikai/actions/workflows/monthly-update.yml"
