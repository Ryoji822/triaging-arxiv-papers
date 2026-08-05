# triaging-arxiv-papers

arXiv の新着から**広告実務に効く論文だけ**を抽出して、平日の朝に Slack へ届ける。

このリポジトリの価値は「たくさん集めること」ではなく「読まなくていい論文を捨てること」にある。
候補を全部並べたレポートは失敗である。

| | |
|---|---|
| 実行 | GitHub Actions で平日 11:00 JST（`0 2 * * 1-5`） |
| 採点 | OpenCode（既定 `zai/glm-5.2`）が S / A / B / C に振り分ける |
| 成果物 | [`reports/`](reports/) に `YYYY-MM-DD.md` を追加 + Slack へ投稿 |

---

## リポジトリ構成

```
.
├── README.md                このファイル
├── opencode.json            OpenCode の設定（プロバイダ・モデル）
├── reports/                 日次レポート（YYYY-MM-DD.md）← 人が読む成果物
├── state/                   仕組みの記憶。ここが育つほど精度が上がる
│   ├── known-topics.md        既知トピック台帳（差分性の採点根拠）
│   ├── seen.json              既出 arXiv ID（同じ論文を二度出さない）
│   ├── runs.json              配信成功した日付（同じ日に二度流さない）
│   ├── slack_posts.json       配信済みの通数（リトライで二重投稿しない）
│   └── fixtures.jsonl         キーワード回帰テスト用の既知論文
├── .github/workflows/
│   └── daily-triage.yml     薄いワークフロー。処理は run-daily.sh に集約
└── .opencode/skills/triaging-arxiv-papers/     ← スキル本体（正本）
    ├── SKILL.md               トリアージの7ステップ
    ├── prompts/daily.md       無人実行で OpenCode に渡すプロンプト
    ├── config/                フィード構成とキーワード
    ├── reference/             採点基準
    └── scripts/
        ├── run-daily.sh         オーケストレータ（これが入口）
        ├── fetch_arxiv.py       取得・重複排除・予備スコア
        ├── fetch_crossref.py    ACM / PVLDB / ACL 側の補完
        ├── post_slack.py        分割・スレッド返信・中断からの再開
        ├── state.py             既出台帳と曜日スケジュール
        ├── selftest.py          キーワードの回帰テスト
        └── selftest_state.py    重複防止の回帰テスト
```

`.claude/skills/` は `.opencode/skills/` への symlink。Claude Code から開いても
同じ実体を編集するので、二重管理にならない。

---

## 動く仕組み

```
fetch_arxiv.py --daily        取得・既出除外・予備スコア（判断はしない）
        ↓
OpenCode + SKILL.md           差分照合・採点・レポート生成・品質チェック
        ↓
post_slack.py                 mrkdwn 変換・分割・スレッド投稿
        ↓
fetch_arxiv.py --mark-seen    既出登録と実行日の記録
        ↓
git commit state/ reports/    次回に持ち越す
```

スクリプトは**決定論的な I/O だけ**を担当し、採否の判断は一切しない。
予備スコアは LLM に渡す件数を絞るためだけのもので、採点根拠に使ってはいけない。

### 取得窓は曜日で変わる

arXiv の告知は平日 20:00 ET に出る。1回の告知が含む投稿日が曜日で違うため、
窓を一律に固定しない。

| 実行日 | 遡る日数 | 理由 |
|--------|---------|------|
| 月 | 4 日 | 日曜の告知は金 14:00 ET 〜 日 14:00 ET の投稿を含む（金・土・日） |
| 火〜金 | 2 日 | 前日ぶんのみ（JST/UTC の日付境界ぶんの余裕を1日) |
| 土・日 | スキップ | 告知そのものがない |

**窓を1日に固定すると月曜に金曜分を静かに落とす。** 重ねた分は `seen.json` が
潰すので、重ねるコストは実質ゼロ。

### 重複を止める4段構え

同じ論文が二度出る経路は複数あるので、経路ごとに別々に止めている。

| 経路 | 止める仕組み |
|------|-------------|
| 取得窓が翌日と重なる | `state/seen.json` — ID 単位の既出除外 |
| 同じ日に再実行される | `state/runs.json` — 配信成功した日付を記録（`TRIAGE_FORCE=1` で上書き） |
| 配信リトライで再送される | `state/slack_posts.json` — 親 ts と配信済み通数から再開 |
| 実行が並走する | ワークフローの `concurrency` で直列化 |

**記録するのは「配信まで成功した日」だけである。** 取得だけして落ちた日を記録すると
その日の論文が二度と出てこない。迷ったら取り逃さない側に倒す設計にしている。

そして **`state/` をコミットし直さないと重複除外は成立しない。** ランナーは毎回
まっさらなので、持ち越さなければ毎朝全件が「新着」として上がる。

---

## セットアップ

### Secrets

| 名前 | 必須 | 用途 |
|------|------|------|
| `GLM_API_KEY` | ✅ | OpenCode のモデル呼び出し |
| `SLACK_BOT_TOKEN` | | 未設定なら配信をスキップし、`reports/` への出力だけで完了扱い |

### Variables

| 名前 | 既定 | 用途 |
|------|------|------|
| `SLACK_CHANNEL` | — | 投稿先（例 `#ai-papers`） |
| `OPENCODE_MODEL` | `zai/glm-5.2` | 採点に使うモデル |

Slack Bot に必要なスコープは `chat:write`（スレッド返信も同じスコープ）。

---

## 手元で動かす

```bash
# 日次実行と同じもの（曜日から窓を決める）
bash .opencode/skills/triaging-arxiv-papers/scripts/run-daily.sh

# 取得だけ試す
python3 .opencode/skills/triaging-arxiv-papers/scripts/fetch_arxiv.py --days 8

# Slack の分割結果だけ確認する（送信しない）
python3 .opencode/skills/triaging-arxiv-papers/scripts/post_slack.py \
  --report reports/2026-08-05.md --dry-run
```

Claude Code から対話的に回す場合は `/triaging-arxiv-papers` を呼ぶ。

### 設定を触ったらテストを通す

取り逃しも重複も**静かに**起きる。レポートを見ただけでは気づけない。

```bash
cd .opencode/skills/triaging-arxiv-papers
python3 scripts/selftest_state.py   # 曜日判定・既出除外・二重配信
python3 scripts/selftest.py         # キーワード（タイトルのみ・下限値）
python3 scripts/selftest.py --live  # arXiv から要旨を取って本番同等で判定
```

`--live` で落ちたものだけが本物の取り逃しである。フィクスチャに過剰適合させないこと
（落ちた論文のタイトルの単語をそのまま足せばテストは通るが、再現率は上がらない）。

---

## 調整するとき

- **候補が多すぎて読み切れない** → `config/queries.json` の `keep_top_per_bucket` を下げる（既定 25）
- **ノイズが多い** → `min_prescore` を 2 に上げる
- **同じ話が毎回上がる** → `state/known-topics.md` が育っていない。ステップ7の追記が飛んでいる
- **S・A が全体の15%を超える** → 採点基準が緩い。`reference/evaluation-criteria.md` の減点根拠を見直す

キーワードは1バケツ12〜15語が目安。増やすと再現率は上がるが予備スコアが飽和して効かなくなる。
汎用語だけを並べたバケツは**ブラックホールになって具体バケツから論文を奪う**。

## 既知の限界

- **ハーネス設計の領域は arXiv の網羅性が低い。** 実務者主導の分野なので OpenReview・ACM DL・
  実務ブログを別経路で見る必要がある。`fetch_crossref.py` で ACM / PVLDB / ACL 側を週次で補える
- **GitHub Actions の cron は遅延・欠落する。** 曜日スキップと配信済み判定があるので
  遅延しても壊れないが、リポジトリが60日無活動になるとスケジュールは自動停止する
- **このリポジトリが public の場合、`reports/` と `state/known-topics.md` は誰でも読める。**
  実務接続の判断がそのまま出るので、社内向けの記述を含めるなら private にする
