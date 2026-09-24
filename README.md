# みらい議会＠遠賀町

福岡県遠賀町議会の会議録をもとに、議事の流れと「誰が何を言い、町がどう答えたか」を、スマートフォンでも読みやすく並べ直す**個人運営の非公式サイト**のソースコードです。

> **お断り**
> - 遠賀町・遠賀町議会が公式に提供しているものではありません。個人が運営しています。
> - **これは政党チームみらいが運営しているものではありません。** 特定の政党・政治団体の立場に立つものではなく、いずれの政党の主張や政策を支持・宣伝するものでもありません。
> - AIによる要約を含みます。正確な内容は必ず原典（遠賀町議会 会議録検索システム）をご確認ください。

## 特徴

- **評価しない。** 議員・会派・町に対する評価、点数付け、ランキング、賛否の色分けをしません。並び順は日付・議案番号・通告順といった機械的なものだけです。議員個人の賛否は、会議録に明記されていない限り推測しません。
- **AIが書いた部分は見分けられる。** AI要約は点線の枠で囲み、要点ごとに根拠となった発言の原文へリンクします。
- **AIの要約を機械で確かめてから載せる。** 要約に出てくる数字が原文にあるか、引用した発言が実在するか、一般質問の質問事項の数が通告書と合うかを1件ずつ照合し、合わないものは載せません。
- **議決結果を2つの公式資料で突き合わせる。** 会議録から読み取った議決結果を、町の「審議案件・結果」PDFと照合します。表記が食い違う議案は、どちらかに揃えず両方をそのまま載せます。
- **利用者の情報を集めない。** Cookie・アクセス解析・広告・外部からの読み込みはありません。JavaScriptは検索ページだけで、検索はブラウザの中で行い、検索語はどこにも送りません。
- **静的サイト。** 実行時のサーバー処理もデータベースもありません。収集からサイト生成までをパイプラインで行い、HTML・CSS・JSONを配置するだけです。

## 仕組み

```
会議録検索システム（VOICES）──┐
町HPの一般質問通告書（PDF） ──┼─→ 取得 → 整形 → AI要約 → 照合 → 静的サイト生成
町HPの審議案件・結果（PDF） ──┘   fetch   parse  summarize verify   build
```

```
pipeline/
  common/     取得のマナー（3秒間隔・逐次・連絡先つきUser-Agent）
  fetch/      会議録（voices.py）と町HPのPDF（town.py）の取得
  parse/      会議録の発言分割、発言者の判定、議案の組み立て、一般質問の分割、PDFの読み取り
  summarize/  AI要約（Claude の Batch API）と、要約の機械的な照合
  verify/     議決結果と「審議案件・結果」PDFの突き合わせ
  build/      静的サイトの生成
  tests/      テスト
masters/      発言者マスタ（表記ゆれ・異体字の対応など、人が判断したもの）
site/assets/  CSS と検索用の JavaScript（外部CDNは使わない）
docs/         設計ドラフト・調査報告・運用メモ
state.json    何をどこから取得したかの記録（原文そのものは含まない）
.github/      月次更新・要約の回収・デプロイのワークフロー
```

**取得した原文・整形データ・AI要約は、このリポジトリに含めていません。** 会議録は町・議会の著作物で、こちらがライセンスを付けられる立場にないためです。これらは別の非公開リポジトリで管理し、`data/` に置いて使います。

## 動かし方

Python 3.12 以降。依存ライブラリは2つだけです。

- `pypdf` — 一般質問通告書と審議案件・結果がPDFでしか公開されていないため
- `anthropic` — AI要約（Batch API のバッチ作成・回収と、混雑時の再試行を公式実装に任せるため）

会議録の取得と解析は、標準ライブラリだけで動きます。

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # AI要約を使うときだけ。ANTHROPIC_API_KEY を入れる
```

```bash
# 1. 取得（取得済みはスキップする。深夜帯以外は実行を拒否する）
python -m pipeline.fetch.voices --dry-run    # 何件取りに行くかだけ確認（通信しない）
python -m pipeline.fetch.voices
python -m pipeline.fetch.town

# 2. 整形
python -m pipeline.parse.speakers            # 未判定の発言者は一覧に出る
python -m pipeline.parse.bills
python -m pipeline.parse.threads

# 3. AI要約（生成済みは作り直さない）
python -m pipeline.summarize.run --dry-run   # 件数と入力の大きさだけ確認
python -m pipeline.summarize.run             # 投入して完了まで待つ

# 4. 議決結果の照合
python -m pipeline.verify.results --report data/verify/結果照合.md

# 5. サイト生成（dist/ に出る）
python -m pipeline.build.site
python -m http.server 4173 --directory dist

# テスト
python -m unittest discover -s pipeline/tests -t .
```

**取得のマナーを守ってください。** `pipeline.fetch.voices` は日本時間の0〜6時以外では実行を拒否し、1リクエストごとに3秒以上あけます。動作確認で日中に少しだけ取るときは `--limit 3 --allow-daytime` を付けます。

**AI要約は一度生成したら作り直しません。** 作り直すときは、対象を `--ids` で指定したうえで `--regenerate` を付けます。プロンプトを変えたら `pipeline/summarize/prompt.py` の `PROMPT_VERSION` を上げてください。要約ごとに、使ったモデル・プロンプトの版・生成日時を記録しています。

### GitHub Actions

| ワークフロー | いつ | 何をするか |
|---|---|---|
| `monthly-update.yml` | 毎月2日 深夜 | 取得 → 整形 → 要約を Batch API に投入 |
| `summarize-collect.yml` | 3時間おき | 要約が揃っていれば照合 → ビルド → プレビュー反映 → **Pull Request を作る** |
| `deploy.yml` | main への push | 本番へ反映 |
| `test.yml` | push・PR | テスト |

投入と回収を分けているのは、Batch API の完了までが数分から十数時間と読めず、1ジョブ6時間の上限に収まる保証がないためです。**本番へは自動で出しません。** 月次更新は Pull Request として届き、人が確認してマージしたときだけ反映されます。

使う Secrets は `DATA_REPO_DEPLOY_KEY`（データ用リポジトリ）、`ANTHROPIC_API_KEY`、`XSERVER_HOST` `XSERVER_USER` `XSERVER_SSH_KEY` `XSERVER_PUBLIC_DIR` `XSERVER_PREVIEW_DIR`（デプロイ）です。デプロイ用が未設定のあいだは、ビルドまでで止まります。

## 他の自治体で使うには

会議録検索システム VOICES は全国の議会で使われているため、会議録の取得と解析はほぼそのまま動くはずです。書き換えが必要なのは次のところです。

| 場所 | 内容 |
|---|---|
| `pipeline/fetch/voices.py` の `BASE` | その議会の VOICES の URL |
| `pipeline/build/site.py` の `VOICES_TOP` `VOICES_BODY` | 同上（サイトからのリンク） |
| `pipeline/fetch/town.py` | 町HPから通告書・審議案件結果のPDFをたどる部分。**町HPの作りに合わせた実装**なので、自治体ごとに書き直しが要る |
| `pipeline/build/site.py` の `SITE_NAME` `OPERATOR` `CONTACT` `SOURCE_URL` とページの文言 | サイト名・運営者・連絡先・町名 |
| `pipeline/common/http.py` の `CONTACT_URL` | 取得時に名乗る連絡先 |
| `masters/speakers.json` | その議会の発言者の表記ゆれ（最初は空でよい。未判定は一覧に出る） |
| `.github/workflows/*.yml` | データ用リポジトリの名前 |
| `site/assets/style.css` | 配色 |

会議録の形式（話者の記号、議事日程、一括議題の書き方など）は議会ごとに癖があります。`python -m pipeline.parse.speakers` が出す未判定の一覧と、`pipeline.verify.results` の照合結果を手がかりに直してください。

**「みらい議会」の名称を使う場合は、** 本家 [team-mirai/mirai-gikai](https://github.com/team-mirai/mirai-gikai) の [Fork ガイドライン](https://github.com/team-mirai/mirai-gikai/blob/develop/FORK_GUIDELINES.md) に従い、「みらい議会＠地域名」の形にし、「これは政党チームみらいが運営しているものではありません」を表示してください。このリポジトリは本家のコードを使っていないので fork ではありませんが、名称を借りている以上この扱いに従っています。

## 開発状況

| フェーズ | 内容 | 状態 |
|---|---|---|
| P0 | 構造調査 | 完了 |
| P1 | 会議録の収集 | 完了（対象は令和元年以降） |
| P2 | 整形・発言者マスタ・議案の組み立て | 完了 |
| P3 | 閲覧（会議・一般質問・議案・議員のページ） | 完了 |
| P4 | AI要約と照合 | 完了（要約297件、議決結果の照合636件） |
| P5 | 検索・月次更新・公開 | 検索と月次更新は完了。本番公開は準備中 |

設計の判断とその理由は [docs/設計ドラフト.md](docs/設計ドラフト.md)、開発上のルールは [CLAUDE.md](CLAUDE.md) にあります。

## 誤りの指摘・連絡

サイトの内容の誤り（読み取りの誤り、要約の誤りなど）は、[Issues](https://github.com/Onga-Mirai-Tech/Onga-Mirai-Gikai/issues) か info@onga-mirai-tech.com までお知らせください。

## ライセンス

コードとドキュメントは **[MIT License](LICENSE)** です。

**会議録、一般質問通告書、審議案件・結果は遠賀町・遠賀町議会などの著作物で、このライセンスは及びません。** AI要約も会議録をもとにしたものであり、同様です。

見た目と言葉づかいは [team-mirai/mirai-gikai](https://github.com/team-mirai/mirai-gikai)（AGPL-3.0）を参考にしていますが、コードは流用していません。

## データの出典

- 遠賀町議会 会議録検索システム（VOICES） http://iasb-sv.town.onga.lg.jp/voices/index.asp
- 遠賀町公式ホームページ 一般質問通告書、審議案件・結果
