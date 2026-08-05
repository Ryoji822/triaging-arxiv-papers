#!/usr/bin/env bash
# 日次トリアージのオーケストレータ。GitHub Actions からも手元からも同じものを叩く。
#
#   取得 → (OpenCode で採点・レポート) → 配信 → 既出登録
#
# 既出登録は必ず配信成功の後に行う。順序を入れ替えると、配信が落ちた日の論文が
# 二度と上がってこなくなる。重複を防ぐことより取り逃さないことを優先している。
#
# 環境変数:
#   ARXIV_TRIAGE_HOME  状態と成果物の置き場所（既定: リポジトリのルート）
#   OPENCODE_MODEL     採点に使うモデル（既定: zai/glm-5.2）
#   SLACK_BOT_TOKEN    未設定なら配信をスキップし、レポートのファイル出力で終わる
#   SLACK_CHANNEL      投稿先
#   TRIAGE_DAYS        遡る日数を明示する（既定: 曜日から自動決定）
#   TRIAGE_FORCE       1 なら配信済みの日でも実行する

set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ARXIV_TRIAGE_HOME="${ARXIV_TRIAGE_HOME:-$(cd "$SKILL_DIR/../../.." && pwd)}"
OPENCODE_MODEL="${OPENCODE_MODEL:-zai/glm-5.2}"

STATE_DIR="$ARXIV_TRIAGE_HOME/state"
OUT_DIR="$ARXIV_TRIAGE_HOME/out"
REPORT_DIR="$ARXIV_TRIAGE_HOME/reports"

log()  { printf '[run-daily] %s\n' "$1" >&2; }
fail() { printf '[run-daily] ERROR: %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------- 0. 回帰テスト
# 設定は静かに腐る。取り逃しも重複もレポートを見ただけでは気づけない。
log "回帰テストを実行します"
python3 "$SKILL_DIR/scripts/selftest_state.py" >&2 || fail "状態管理のテストが落ちました"
python3 "$SKILL_DIR/scripts/selftest.py" >&2 || log "警告: キーワードに取り逃しがあります（続行します）"

# ---------------------------------------------------------------- 1. 取得
args=(--daily)
[ -n "${TRIAGE_DAYS:-}" ] && args+=(--days "$TRIAGE_DAYS")
[ "${TRIAGE_FORCE:-0}" = "1" ] && args+=(--force)

log "候補を取得します: ${args[*]}"
python3 "$SKILL_DIR/scripts/fetch_arxiv.py" "${args[@]}" >&2 || fail "取得に失敗しました"

read_meta() { python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$OUT_DIR/run_meta.json" "$1"; }
STATUS="$(read_meta status)"
RUN_DATE="$(read_meta date)"
CANDIDATES="$(read_meta candidates)"
WINDOW="$(read_meta window)"

log "status=$STATUS date=$RUN_DATE candidates=$CANDIDATES"
if [ "$STATUS" != "ok" ]; then
  log "この実行では配信しません（$STATUS）"
  exit 0
fi

# ---------------------------------------------------------------- 2. 採点とレポート
mkdir -p "$REPORT_DIR"
REPORT="$REPORT_DIR/$RUN_DATE.md"

PROMPT="$(sed \
  -e "s|__RUN_DATE__|$RUN_DATE|g" \
  -e "s|__WINDOW__|$WINDOW|g" \
  -e "s|__CANDIDATES__|$CANDIDATES|g" \
  -e "s|__OUT_DIR__|$OUT_DIR|g" \
  -e "s|__STATE_DIR__|$STATE_DIR|g" \
  -e "s|__SKILL_DIR__|$SKILL_DIR|g" \
  -e "s|__REPORT__|$REPORT|g" \
  "$SKILL_DIR/prompts/daily.md")"

log "OpenCode で採点します（model=$OPENCODE_MODEL）"
if ! timeout "${TRIAGE_TIMEOUT:-2700}" opencode run --model "$OPENCODE_MODEL" "$PROMPT" >&2; then
  fail "OpenCode の実行に失敗しました"
fi
[ -s "$REPORT" ] || fail "レポートが生成されませんでした: $REPORT"
log "レポートを生成しました: $REPORT ($(wc -c <"$REPORT" | tr -d ' ') バイト)"

# ---------------------------------------------------------------- 3. 配信
python3 "$SKILL_DIR/scripts/post_slack.py" --report "$REPORT" --date "$RUN_DATE" >&2
case "$?" in
  0) log "Slack に配信しました" ;;
  2) log "警告: Slack 未設定のため、レポートのファイル出力のみで配信完了とみなします" ;;
  *) fail "Slack 配信に失敗しました。既出登録はしません（翌日やり直します）" ;;
esac

# ---------------------------------------------------------------- 4. 既出登録
# ここまで来た日だけ既出として登録し、実行日を記録する。
log "既出登録と実行日の記録を行います"
python3 "$SKILL_DIR/scripts/fetch_arxiv.py" --mark-seen >&2 || fail "既出登録に失敗しました"

log "完了: $RUN_DATE"
