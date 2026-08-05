#!/usr/bin/env python3
"""トリアージレポート（Markdown）を Slack に投稿する。無人実行用。

対話実行では Slack の MCP ツールを使えばよい。このスクリプトが必要なのは
GitHub Actions のような無人環境で、かつ「途中で失敗しても二重投稿しない」
ことを保証したい場合である。

二重投稿を防ぐ仕組み:
  投稿のたびに state/slack_posts.json に日付ごとの親メッセージ ts と
  「何通目まで配信済みか」を記録する。再実行時は記録を読み、親を作り直さず
  未配信の通からスレッド返信を続ける。ネットワーク断でワークフローが
  リトライされても、既に届いた通は二度送られない。

トークンは環境変数からのみ読む（SLACK_BOT_TOKEN）。ログには出さない。
標準ライブラリのみで動作する（pip install 不要）。

  python3 scripts/post_slack.py --report out/report.md --channel '#ai-papers'
  python3 scripts/post_slack.py --report out/report.md --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from state import (  # noqa: E402
    RunLedger,
    data_home,
    read_json,
    today_in,
    write_json_atomic,
)

ROOT = Path(__file__).resolve().parent.parent  # スキル本体
HOME = data_home(ROOT)  # 状態ファイル（ARXIV_TRIAGE_HOME で移せる）

CONFIG_PATH = ROOT / "config" / "queries.json"
POSTS_PATH = HOME / "state" / "slack_posts.json"
API = "https://slack.com/api/chat.postMessage"

CHUNK_LIMIT = 3800  # Slack の1メッセージ上限4,000文字に対する安全側の値


def log(msg: str) -> None:
    print(f"[post_slack] {msg}", file=sys.stderr)


# ---------------------------------------------------------------- mrkdwn

def to_mrkdwn(md: str) -> str:
    """Markdown を Slack の mrkdwn に変換する。表は箇条書きに落とす。"""
    out = []
    for line in md.splitlines():
        if re.match(r"^\s*\|[\s|:-]+\|\s*$", line):
            continue  # 表の区切り行は捨てる
        if re.match(r"^\s*\|.*\|\s*$", line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            line = "• " + " — ".join(c for c in cells if c)
        line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", line)
        line = re.sub(r"\*\*([^*]+)\*\*", r"*\1*", line)
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            line = f"*{m.group(2).strip().strip('*')}*"
        line = re.sub(r"^(\s*)-\s+", r"\1• ", line)
        out.append(line)
    return "\n".join(out)


def split_messages(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """セクション境界（*見出し* 行）を優先して分割する。行の途中で切らない。"""
    blocks, current = [], []
    for line in text.splitlines():
        is_heading = bool(re.match(r"^\*[^*]+\*$", line.strip()))
        if is_heading and current:
            blocks.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        blocks.append("\n".join(current))

    messages, buf = [], ""
    for block in blocks:
        for piece in _hard_split(block, limit):
            if buf and len(buf) + len(piece) + 2 > limit:
                messages.append(buf.strip())
                buf = piece
            else:
                buf = f"{buf}\n\n{piece}" if buf else piece
    if buf.strip():
        messages.append(buf.strip())
    return messages


def _hard_split(block: str, limit: int) -> list[str]:
    """1セクションが上限を超える場合だけ行単位で割る。"""
    if len(block) <= limit:
        return [block]
    parts, buf = [], ""
    for line in block.splitlines():
        if buf and len(buf) + len(line) + 1 > limit:
            parts.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        parts.append(buf)
    return parts


# ---------------------------------------------------------------- slack api

def post(token: str, channel: str, text: str, thread_ts: str | None) -> str:
    payload = {"channel": channel, "text": text, "unfurl_links": False}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Slack への接続に失敗しました: {exc}") from exc
    if not body.get("ok"):
        # エラー本文にトークンは含まれない。error コードだけを出す
        raise RuntimeError(f"Slack API エラー: {body.get('error')}")
    return body.get("ts") or ""


# ---------------------------------------------------------------- main

def deliver(messages: list[str], token: str, channel: str, day: date) -> dict:
    """未配信の通だけを送る。親が既にあればスレッド返信から再開する。"""
    posts = read_json(POSTS_PATH, {"days": {}})
    entry = posts.setdefault("days", {}).get(day.isoformat(), {})
    parent_ts = entry.get("parent_ts")
    delivered = int(entry.get("delivered", 0))

    if delivered >= len(messages):
        log(f"{day.isoformat()} は全 {delivered} 通を配信済み。何もしません")
        return entry

    for i, msg in enumerate(messages):
        if i < delivered:
            continue
        ts = post(token, channel, msg, None if i == 0 else parent_ts)
        if i == 0:
            parent_ts = ts
        delivered = i + 1
        entry = {"channel": channel, "parent_ts": parent_ts, "delivered": delivered,
                 "total": len(messages)}
        posts["days"][day.isoformat()] = entry
        write_json_atomic(POSTS_PATH, posts)  # 1通ごとに永続化する
        log(f"{i + 1}/{len(messages)} 通目を送信しました")
    return entry


def main() -> int:
    p = argparse.ArgumentParser(description="トリアージレポートを Slack に投稿する")
    p.add_argument("--report", required=True, help="Markdown レポートのパス")
    p.add_argument("--channel", default=os.environ.get("SLACK_CHANNEL"),
                   help="投稿先（既定は環境変数 SLACK_CHANNEL）")
    p.add_argument("--date", default=None, help="配信日 YYYY-MM-DD（既定は設定TZの今日）")
    p.add_argument("--dry-run", action="store_true", help="送信せず分割結果だけ表示する")
    p.add_argument("--mark-run", action="store_true",
                   help="配信成功を state/runs.json にも記録する")
    args = p.parse_args()

    report = Path(args.report)
    if not report.exists():
        log(f"レポートが見つかりません: {report}")
        return 1

    cfg = read_json(CONFIG_PATH, {"daily_schedule": {}})
    day = (date.fromisoformat(args.date) if args.date
           else today_in(cfg.get("daily_schedule", {}).get("timezone")))
    messages = split_messages(to_mrkdwn(report.read_text(encoding="utf-8")))
    if not messages:
        log("レポートが空です。送信しません")
        return 1

    if args.dry_run:
        for i, m in enumerate(messages, 1):
            print(f"--- {i}/{len(messages)} 通目 ({len(m)} 文字) ---\n{m}\n")
        return 0

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        log("SLACK_BOT_TOKEN が未設定です。送信をスキップします（レポートは保存済み）")
        return 2
    if not args.channel:
        log("投稿先が未指定です（--channel か SLACK_CHANNEL）。送信をスキップします")
        return 2

    entry = deliver(messages, token, args.channel, day)
    if args.mark_run:
        RunLedger(HOME / "state" / "runs.json").record(
            day, delivered="slack", messages=entry.get("delivered", 0)
        )
    log(f"完了: {entry.get('delivered')}/{entry.get('total')} 通")
    return 0


if __name__ == "__main__":
    sys.exit(main())
