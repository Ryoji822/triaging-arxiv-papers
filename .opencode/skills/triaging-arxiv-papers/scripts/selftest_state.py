#!/usr/bin/env python3
"""状態管理と日次スケジュールの回帰テスト。

ここが壊れると「同じ論文が毎朝上がる」「金曜分を静かに落とす」という形で
劣化する。どちらもレポートを見ただけでは気づけないので、設定やスクリプトを
触ったら必ず回す。取り逃しと同じく、重複も静かに起きる。

  python3 scripts/selftest_state.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from state import (  # noqa: E402
    RunLedger,
    SeenStore,
    read_json,
    resolve_daily_window,
    write_json_atomic,
)

SCHEDULE = {
    "timezone": "Asia/Tokyo",
    "skip_weekdays": ["sat", "sun"],
    "lookback_days": {"mon": 4, "default": 2},
}

failures: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual == expected:
        print(f"  ok   {label}")
    else:
        print(f"  NG   {label}: {actual!r} != {expected!r}")
        failures.append(label)


# ---------------------------------------------------------------- schedule

def test_schedule() -> None:
    print("=== 曜日ごとの取得窓 ===")
    # 2026-08-03 は月曜。金土日の投稿を拾うため窓を広く取る
    run, days, _ = resolve_daily_window(SCHEDULE, date(2026, 8, 3))
    check("月曜は実行する", run, True)
    check("月曜の窓は4日", days, 4)

    for day, label in [(date(2026, 8, 4), "火"), (date(2026, 8, 7), "金")]:
        run, days, _ = resolve_daily_window(SCHEDULE, day)
        check(f"{label}曜は実行する", run, True)
        check(f"{label}曜の窓は2日", days, 2)

    for day, label in [(date(2026, 8, 8), "土"), (date(2026, 8, 9), "日")]:
        run, days, reason = resolve_daily_window(SCHEDULE, day)
        check(f"{label}曜はスキップする", run, False)
        check(f"{label}曜はスキップ理由が出る", bool(reason), True)

    # 窓の合計が週を覆っているか（月4日 + 平日2日×4 で金→月の穴がない）
    covered = set()
    for d in [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5),
              date(2026, 8, 6), date(2026, 8, 7)]:
        _, days, _ = resolve_daily_window(SCHEDULE, d)
        covered |= {date.fromordinal(d.toordinal() - k) for k in range(days)}
    # 前週金曜(7/31)から当週金曜(8/7)まで連続して覆われていること
    expected_span = {date.fromordinal(date(2026, 7, 31).toordinal() + k) for k in range(8)}
    check("金曜〜金曜に取得窓の穴がない", expected_span - covered, set())


# ---------------------------------------------------------------- seen store

def test_seen_store(tmp: Path) -> None:
    print("=== 既出ストア ===")
    path = tmp / "seen.json"
    store = SeenStore(path)
    check("初回は空", len(store), 0)
    check("追加件数を返す", store.add(["2608.01679", "2608.01637"]), 2)
    check("同じIDを二度足しても増えない", store.add(["2608.01679"]), 0)
    store.save()

    reloaded = SeenStore(path)
    check("保存内容が読み戻せる", len(reloaded), 2)
    check("既出判定が効く", "2608.01679" in reloaded, True)
    check("未知のIDは既出でない", "2608.99999" in reloaded, False)

    # 保持期間の刈り取り: 2026-08 から4か月前は 2026-04
    aged = SeenStore(tmp / "aged.json")
    aged.add(["2603.00001", "2604.00001", "2608.00001", "10.1145/1234567"])
    dropped = aged.prune(retention_months=4, today=date(2026, 8, 5))
    check("4か月より古いIDを捨てる", dropped, 1)
    check("境界月(2604)は残す", "2604.00001" in aged, True)
    check("古い月(2603)は消える", "2603.00001" in aged, False)
    check("DOI形式は判定せず残す", "10.1145/1234567" in aged, True)
    check("刈り取り0指定では何もしない", aged.prune(0, date(2026, 8, 5)), 0)


# ---------------------------------------------------------------- run ledger

def test_run_ledger(tmp: Path) -> None:
    print("=== 実行台帳（同日二重配信の防止） ===")
    path = tmp / "runs.json"
    ledger = RunLedger(path)
    today = date(2026, 8, 5)
    check("未実行の日は completed でない", ledger.completed(today), False)
    ledger.record(today, candidates=84, newly_seen=84)
    check("記録後は completed", RunLedger(path).completed(today), True)
    check("翌日はまだ未実行", RunLedger(path).completed(date(2026, 8, 6)), False)
    check("記録内容が残る", RunLedger(path).entry(today)["candidates"], 84)


# ---------------------------------------------------------------- durability

def test_durability(tmp: Path) -> None:
    print("=== 状態ファイルの堅牢性 ===")
    path = tmp / "atomic.json"
    write_json_atomic(path, {"ids": ["a"]})
    write_json_atomic(path, {"ids": ["a", "b"]})
    check("上書きできる", json.loads(path.read_text())["ids"], ["a", "b"])
    check("一時ファイルが残らない", list(tmp.glob(".atomic.json.*")), [])

    broken = tmp / "broken.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    check("壊れたJSONで既定値を返す", read_json(broken, {"ids": []}), {"ids": []})
    check("壊れたファイルでも SeenStore は起動する", len(SeenStore(broken)), 0)


# ---------------------------------------------------------------- slack

def test_slack_resume(tmp: Path) -> None:
    """途中で失敗した配信を再開したとき、届いた通を二度送らないこと。"""
    print("=== Slack 配信の再開（二重投稿の防止） ===")
    import post_slack

    post_slack.POSTS_PATH = tmp / "slack_posts.json"
    messages = ["親メッセージ", "A評価", "B評価"]
    day = date(2026, 8, 5)
    sent: list[tuple[str, str | None]] = []

    def flaky(token, channel, text, thread_ts):
        if text == "A評価" and not getattr(flaky, "healed", False):
            flaky.healed = True
            raise RuntimeError("Slack API エラー: ratelimited")
        sent.append((text, thread_ts))
        return "1754_" + str(len(sent))

    post_slack.post = flaky
    try:
        post_slack.deliver(messages, "xoxb-dummy", "#test", day)
    except RuntimeError:
        pass
    check("1通目だけ届いている", [t for t, _ in sent], ["親メッセージ"])

    post_slack.deliver(messages, "xoxb-dummy", "#test", day)
    check("再開後は3通そろう", [t for t, _ in sent], ["親メッセージ", "A評価", "B評価"])
    check("親を作り直していない", [t for t, _ in sent].count("親メッセージ"), 1)
    check("2通目以降はスレッド返信", [ts for _, ts in sent[1:]], ["1754_1", "1754_1"])

    post_slack.deliver(messages, "xoxb-dummy", "#test", day)
    check("全通配信済みなら何も送らない", len(sent), 3)


def test_dm_target(tmp: Path) -> None:
    """DM（ユーザーID）とチャンネルを見分け、DM だけ conversations.open で解決する。"""
    print("=== 投稿先の解決（DM 対応） ===")
    import post_slack

    check("ユーザーIDを判定する", post_slack.is_user_id("U012ABCDEF"), True)
    check("Enterprise Grid の W も判定する", post_slack.is_user_id("W012ABCDEF"), True)
    check("公開チャンネルIDは誤判定しない", post_slack.is_user_id("C012ABCDEF"), False)
    check("DMチャンネルIDは誤判定しない", post_slack.is_user_id("D012ABCDEF"), False)
    check("#名前は誤判定しない", post_slack.is_user_id("#ai-papers"), False)
    check("前後の空白を無視する", post_slack.is_user_id(" U012ABCDEF "), True)

    calls: list[tuple[str, dict]] = []

    def fake_call(token, method, payload):
        calls.append((method, payload))
        return {"ok": True, "channel": {"id": "D999XYZ"}}

    post_slack.call = fake_call
    check("ユーザーIDはDMに解決される",
          post_slack.resolve_target("xoxb-dummy", "U012ABCDEF"), "D999XYZ")
    check("conversations.open を呼ぶ", calls[0][0], "conversations.open")
    check("users にユーザーIDを渡す", calls[0][1], {"users": "U012ABCDEF"})

    calls.clear()
    check("チャンネルIDはそのまま",
          post_slack.resolve_target("xoxb-dummy", "C012ABCDEF"), "C012ABCDEF")
    check("チャンネルでは API を叩かない", calls, [])


def test_anchor(tmp: Path) -> None:
    """親メッセージは短く、本文は全部スレッドに回ること。"""
    print("=== 親メッセージ（アンカー） ===")
    import post_slack

    md = ("# arXiv 論文トリアージレポート\n"
          "**対象期間：2026年8月3日〜2026年8月5日**\n\n"
          "## S評価\n本文本文\n\n"
          "## 収集メタデータ\n- S：3件 / A：7件 / B：15件 / C：59件\n")
    anchor = post_slack.build_anchor(md, date(2026, 8, 5), replies=3)
    check("対象期間が入る", "2026年8月3日〜2026年8月5日" in anchor, True)
    check("件数が入る", "S 3件 / A 7件 / B 15件" in anchor, True)
    check("スレッド通数が入る", "3通" in anchor, True)
    check("短い（300文字未満）", len(anchor) < 300, True)
    check("本文は入らない", "本文本文" in anchor, False)

    bare = post_slack.build_anchor("# タイトルのみ\n中身\n", date(2026, 8, 5), replies=1)
    check("期間も件数も無くても壊れない", bare.startswith(":books:"), True)


def test_split(tmp: Path) -> None:
    print("=== レポートの分割 ===")
    import post_slack

    md = "# 見出し\n本文\n\n## S評価\n" + "S行\n" * 40 + "\n## A評価\n" + "A行\n" * 40
    msgs = post_slack.split_messages(post_slack.to_mrkdwn(md), limit=200)
    check("上限を超える通がない", [m for m in msgs if len(m) > 200], [])
    check("複数通に分かれる", len(msgs) > 1, True)
    check("行の途中で切っていない", all("行行" not in m for m in msgs), True)
    joined = "".join(msgs)
    check("内容が落ちていない", joined.count("S行"), 40)


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_schedule()
        test_seen_store(tmp)
        test_run_ledger(tmp)
        test_durability(tmp)
        test_slack_resume(tmp)
        test_dm_target(tmp)
        test_anchor(tmp)
        test_split(tmp)

    print()
    if failures:
        print(f"失敗 {len(failures)} 件: {', '.join(failures)}")
        return 1
    print("すべて通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
