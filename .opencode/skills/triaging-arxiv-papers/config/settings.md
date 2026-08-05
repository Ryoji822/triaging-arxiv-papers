# 設定

機械可読な設定は [queries.json](queries.json)。このファイルはその読み方と調整方法。

## フィードの2階層構成

arXiv の取得口は2つあり、役割が違う。

**1. カテゴリRSS（再現率重視・全部流れてくる）**

```
https://rss.arxiv.org/rss/cs.CL          # RSS 2.0
https://rss.arxiv.org/atom/cs.CL         # Atom
https://rss.arxiv.org/rss/cs.AI+cs.HC    # 複数カテゴリは + で連結（上限2000件）
```

その日に発表された全件が流れる。cs.CL や cs.CV は1日あたり数百件régularly出るので、RSSリーダーで人間が読むものではない。`arxiv:announce_type` に `new` / `cross` / `replace` / `replace-cross` が入るので、改訂版を落とせる。

**2. API キーワード検索（精度重視・自分で絞る）**

```
https://export.arxiv.org/api/query?search_query=<クエリ>&sortBy=submittedDate&sortOrder=descending&max_results=100
```

Atom で返るので、そのままRSSリーダーにも入れられる。フィールド接頭辞は `ti:` `abs:` `au:` `cat:` `all:`、論理演算子は大文字の `AND` / `OR` / `ANDNOT`、複数語のフレーズは二重引用符で囲む。ワイルドカードは使えない。**3秒に1リクエスト**を守ること（利用規約）。

このスキルは API を主、RSS を `--include-rss` で補助として使う。API は語彙一致なので言い換えを取り逃す（例: "mental world model" は "theory of mind" では拾えても "belief" 単独では拾えない）。取り逃しが気になる分野は RSS を足して、絞り込みを LLM 側に寄せる。

## バケツ（興味領域）

| バケツ | arXiv カテゴリ | RSS 補助 |
|--------|---------------|----------|
| AIの心理学応用 | cs.CL, cs.HC, cs.AI | cs.CL+cs.HC |
| マーケティング調査 | cs.CL, cs.HC, econ.GN, stat.AP | econ.GN+stat.AP |
| データ分析 | stat.ME, stat.ML, cs.LG, econ.EM | stat.ME+econ.EM |
| エージェント制御 | cs.AI, cs.MA, cs.SE | cs.MA+cs.SE |
| 広告クリエイティブ生成 | cs.CV, cs.GR, cs.MM | cs.MM+cs.GR |
| 同僚としてのエージェント | cs.HC, cs.MA, cs.CY | cs.HC+cs.CY |
| セマンティックレイヤーとオントロジー運用 | cs.DB, cs.IR, cs.AI, cs.SE | cs.DB+cs.IR |
| エージェントの長期記憶 | cs.CL, cs.IR, cs.AI, cs.HC | cs.CL+cs.IR |
| エージェント・ハーネス設計 | cs.SE, cs.AI, cs.CR, cs.MA | cs.SE+cs.CR |

レポート上ではこの日本語ラベルを使う。`psych` `agentctl` 等の内部IDは出さない。

### カテゴリ選定の考え方

- **cs.AI を主軸にしない。** 「他のどこにも入らないもの」の受け皿なので、単体では雑音が多い。必ずキーワードとのANDで使う
- セマンティックレイヤー／オントロジー運用の本流は **cs.DB**（データ統合・エンティティ解決・スキーマ進化・リネージ）。cs.LO は形式論理の伝統側なので入れない
- エージェント記憶の本流は **cs.CL**。cs.AI は副次
- 「Palantir」で検索しても何も出ない。学術側の語彙は semantic layer / context graph / enterprise knowledge graph / digital twin / entity resolution

## 除外キーワード（exclude_keywords）

一致すると予備スコアを1件あたり-2点する。**ハードな除外ではない**（候補には残り、最終判断はLLMが行う）。

用途は「同じ語彙を使う別の研究伝統」を押し下げること。例:

- オントロジー運用バケツ → `description logic` `decidability` `first-order rewriting` 等で形式論理側の論文を沈める。OWL・RDF・SPARQL は実務側でも使う語なので**除外語にしない**
- 長期記憶バケツ → `KV cache` `attention sink` 等で推論最適化の論文を沈める（記憶の設計ではなく実装効率の話なので）

正の一致が1つも無いバケツは、その論文の主バケツになれない。減点だけを免れたバケツに論文が流れるのを防ぐため。

## しきい値の調整

`queries.json` の `defaults`:

| キー | 既定 | 意味 |
|------|------|------|
| `lookback_days` | 2 | 手動実行で遡る日数。週次なら8にする（`--daily` では下記の曜日別設定が優先） |
| `max_results_per_bucket` | 120 | API から取る上限 |
| `keep_top_per_bucket` | 25 | 予備スコア上位いくつを LLM に渡すか。**候補が多すぎて読み切れないときはここを下げる** |
| `min_prescore` | 1 | 予備スコアの下限。ノイズが多いときは2に上げる |
| `seen_retention_months` | 4 | 既出IDを覚えておく期間。取得窓は数日しかないので数か月で十分 |

## 日次実行（曜日で取得窓を変える理由）

`queries.json` の `daily_schedule`:

```json
{
  "timezone": "Asia/Tokyo",
  "skip_weekdays": ["sat", "sun"],
  "lookback_days": { "mon": 4, "default": 2 }
}
```

arXiv の告知は平日 20:00 ET に出る。1回の告知が含む投稿日は曜日で違う。

| 告知 | 含まれる投稿 | published の日付 |
|------|------------|-----------------|
| 日曜 20:00 ET（= 月曜 10:00 JST) | 金 14:00 ET 〜 日 14:00 ET | 金・土・日 |
| 月〜木 20:00 ET | 前日 14:00 ET 〜 当日 14:00 ET | 前日・当日 |

つまり**月曜の実行だけは金曜まで遡らないと落ちる**。全曜日を一律に広げると
無駄な取得が増えるので、月曜だけ4日、火〜金は2日（JST/UTC の日付境界ぶんの余裕）
にしている。**土日は告知がないので走らせない。**

窓が重なった分は `state/seen.json` が潰すので、重ねるコストは実質ゼロである。
逆に窓を1日に固定すると、月曜に金曜分を**静かに**落とす。取り逃しと同じで、
レポートを見ただけでは気づけない。

cron は `0 2 * * 1-5`（= 11:00 JST 平日）にする。08:00 JST では当日の告知
（10:00 JST）より前になり、前営業日ぶんしか取れない。

## 重複を止める4つの状態ファイル

| ファイル | 止める重複 | 消したときに起きること |
|---------|-----------|---------------------|
| `state/seen.json` | 取得窓の重なりによる再掲 | 過去論文が再浮上する（棚卸し時以外は消さない） |
| `state/runs.json` | 同じ日の二重配信 | 同日の再実行が止まらなくなる（`--force` 相当） |
| `state/slack_posts.json` | 配信リトライによる二重投稿 | 途中失敗後の再送で親メッセージが増える |
| `state/known-topics.md` | 同じ論点の再掲（差分性の採点根拠） | 毎週同じ話が上位に来てフィードが死ぬ |

**記録するのは「配信まで成功した日」だけである。** 取得だけして落ちた日を
記録すると、その日の論文が二度と出てこない。重複を防ぐことと取り逃しを
防ぐことは両立させる必要があり、迷ったら取り逃さない側に倒す。

CI で走らせる場合、この4ファイルは**毎回コミットして持ち越す**。ephemeral な
ランナーで状態が消えると、毎朝全件が新着として上がる。

キーワードを増やすと再現率は上がるが予備スコアが飽和して効かなくなる。1バケツ12-15語を目安に、効いていない語を入れ替える運用にする。

## キーワードの回帰テスト（重要）

継続運用でいちばん最初に腐るのはキーワードである。しかも取り逃しは**静かに0件になるだけ**で誰も気づかない。だから既知の良い論文を `state/fixtures.jsonl` に固定し、設定を触るたびに回す。

```bash
python3 scripts/selftest.py          # タイトルのみ（オフライン・下限値）
python3 scripts/selftest.py --live   # arXiv から要旨を取得して本番同等で判定
```

取り逃しがあれば終了コード1を返すので、CIやコミットフックから回せる。

**フィクスチャに過剰適合させないこと。** 落ちた論文のタイトルにある単語をそのまま足せばテストは通るが、実際の再現率は上がらない。追加するのは分野の語彙として複数の論文に出てくる語だけにする。タイトルのみのテストは下限なので、`--live` で落ちたものだけが本物の取り逃しである。

### 予備スコアの語数重み付け

キーワードは語数で重み付けされる（要旨一致=語数、タイトル一致=語数×2）。`multi-agent` のような汎用語より `tool call interception` のような具体語が高くなる。

これは実測で必要になった修正である。汎用語だけを並べたバケツは**ブラックホールになって具体バケツから論文を奪う**。エージェント制御バケツに `LLM agent` `multi-agent` `tool use` `guardrail` を入れていたら、ハーネス設計と長期記憶の論文6本を全部吸い込んだ。バケツを足すときは、既存バケツの語彙と衝突しないか必ずテストで確認する。

## arXiv だけでは足りない領域がある

ハーネス設計バケツは特にそうで、この分野は実務者主導なので arXiv の網羅性が低い。実測した例：

- 最も強い主張（ハーネス変更だけでベンチマークが最大10倍）を出しているサーベイは **OpenReview** にあり arXiv にない
- 実在する Claude Code プロジェクトの設定を調べた実証研究は **ACM**（Agentic Engineering ワークショップ）
- Anthropic / OpenAI のハーネス設計ガイドはブログ

この領域を継続的に追うなら、arXiv フィードに加えて OpenReview、ACM DL、実務ブログ、更新の速い awesome 系リポジトリを別経路で見る必要がある。arXiv だけを見て「今週は0件」と判断すると実際には見落としている。

## ACM 以外も含めて Crossref 経由で取る

ACM Digital Library には公開APIがない（APIキー発行不可、機関認証かWeb UIのみ、スクレイピングはIPブロック）。ACM会員であってもAPIは開かない。

ただし出版社は DOI を Crossref に登録するので、そこからメタデータが取れる。無認証・無料で日付フィルタが使える。DOI接頭辞で出版社を指定する。

```bash
python3 scripts/fetch_crossref.py --days 7 --mail you@example.com
python3 scripts/fetch_crossref.py --days 30 --publisher acm pvldb --mail you@example.com
python3 scripts/fetch_crossref.py --days 14 --venue "ICSE" --mail you@example.com
```

`config/queries.json` の `sources.publishers` で管理する：

| キー | 接頭辞 | 主にどのバケツに効くか | 既定 |
|------|--------|----------------------|------|
| acm | 10.1145 | ハーネス設計、オントロジー（SIGMOD, CIKM, The Web Conf） | 有効 |
| pvldb | 10.14778 | **オントロジー運用**（データ統合、エンティティ解決、NL2SQL） | 有効 |
| ieee | 10.1109 | ハーネス設計、オントロジー（ICDE, ICSE共催分） | 有効 |
| acl | 10.18653 | **長期記憶、AIの心理学応用**（ACL, EMNLP, NAACL） | 有効 |
| springer | 10.1007 | 汎用。量が多いので既定は無効 | 無効 |

**PVLDB は ACM DL 上にあるが DOI接頭辞が違う。** VLDB Endowment 発行なので 10.14778 で、`prefix:10.1145` では丸ごと漏れる。データ統合・エンティティ解決・セマンティックレイヤー系の最上位会議なので、オントロジーバケツにはここが必須である。

出力は `out/candidates_acm.{jsonl,md}` で、arXiv 側と同じ形式なので同じ評価基準にかけられる。既出は `state/seen_acm.json` で別管理する（DOIとarXiv IDは名前空間が違うため）。

`--mail` は付けること。Crossref は連絡先付きリクエストを「礼儀正しい利用者」用のサーバプールに回すので、無指定だと共有プールで不安定になる。

**制約を2つ理解しておく。**

1. **Crossref に要旨がない論文がある。** 出版社が要旨を必ず登録するわけではない。要旨がなければタイトルしか一致判定に使えないので、その分だけ取り逃しが増える。件数と出版社別内訳はログに出るので、比率が高い領域は `--venue` で会議を直接指定して補う
2. **`from-created-date` は登録日であって出版日ではない。** 古い論文がメタデータ更新で浮上することがある。日次ではなく週次で回し、既出除外に頼るのが現実的

2026年1月から ACM DL は全編オープンアクセスなので、ACM分については本文の閲覧に購読は不要である。

### 会議単位で追うなら DBLP

特定の会議（ICSE、CHI、SIGMOD、各ワークショップ）を狙うなら DBLP のほうが速く正確である。

```
https://dblp.org/search/publ/api?q=<query>&format=json&h=100
```

要旨は返らないので、タイトルで拾って DOI から本文に飛ぶ運用になる。

## 状態ファイル

- `state/seen.json` — 既出arXiv ID。同じ論文が二度上がらないようにする
- `state/known-topics.md` — S・A評価した論文の主張1行ログ。**差分性の採点根拠**。ここが育つほど精度が上がる

`seen.json` を消すと過去論文が再浮上する。棚卸しをしたいとき以外は消さない。
