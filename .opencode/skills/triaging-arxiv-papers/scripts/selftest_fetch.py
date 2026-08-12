#!/usr/bin/env python3
"""arXiv 取得のリトライと隔離の回帰テスト。

ここが壊れると「arXiv が一時的に混んでいるだけで日次配信が丸ごと落ちる」
という形で劣化する（2026-08-12 の HTTP 429 連発による実障害が動機）。
外部にアクセスせず、http_get / collect の失敗時の振る舞いだけを検証する。

  python3 scripts/selftest_fetch.py
"""

from __future__ import annotations

import argparse
import io
import sys
import urllib.error
from email.message import Message
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_arxiv  # noqa: E402

failures: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual == expected:
        print(f"  ok   {label}")
    else:
        print(f"  NG   {label}: {actual!r} != {expected!r}")
        failures.append(label)


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("http://x", code, "err", headers, io.BytesIO(b""))


# ---------------------------------------------------------------- retry wait

def test_retry_wait() -> None:
    print("=== リトライの待ち時間 ===")
    plain = urllib.error.URLError("timed out")
    check("通常エラーは指数バックオフ",
          [fetch_arxiv.retry_wait(a, plain) for a in range(4)],
          [5.0, 10.0, 20.0, 40.0])
    check("待ち時間は上限を超えない",
          fetch_arxiv.retry_wait(10, plain), fetch_arxiv.RETRY_WAIT_CAP)
    check("429は最低30秒待つ",
          fetch_arxiv.retry_wait(0, http_error(429)) >= 30.0, True)
    check("503も最低30秒待つ",
          fetch_arxiv.retry_wait(0, http_error(503)) >= 30.0, True)
    check("Retry-After を尊重する",
          fetch_arxiv.retry_wait(0, http_error(429, "90")), 90.0)
    check("Retry-After が長すぎても付き合わない",
          fetch_arxiv.retry_wait(0, http_error(429, "9999")),
          fetch_arxiv.RETRY_AFTER_MAX)
    check("壊れた Retry-After は無視する",
          fetch_arxiv.retry_wait(0, http_error(429, "soon")) >= 30.0, True)


# ---------------------------------------------------------------- http_get

class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_http_get() -> None:
    print("=== http_get のリトライ ===")
    waits: list[float] = []
    orig_urlopen, orig_sleep = fetch_arxiv._urlopen, fetch_arxiv._sleep
    fetch_arxiv._sleep = waits.append
    try:
        calls = {"n": 0}

        def recover_after_2(req, timeout=0):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise http_error(429)
            return FakeResponse(b"payload")

        fetch_arxiv._urlopen = recover_after_2
        check("429のあと回復すれば結果を返す",
              fetch_arxiv.http_get("http://x", "ua"), b"payload")
        check("回復するまで再試行する", calls["n"], 3)
        check("429の待ちは30秒以上", all(w >= 30 for w in waits), True)

        calls["n"] = 0
        waits.clear()
        fetch_arxiv._urlopen = lambda req, timeout=0: FakeResponse(b"ok")
        check("成功すれば1回で返す",
              fetch_arxiv.http_get("http://x", "ua"), b"ok")
        check("成功時は待たない", waits, [])

        def always_429(req, timeout=0):
            calls["n"] += 1
            raise http_error(429)

        calls["n"] = 0
        waits.clear()
        fetch_arxiv._urlopen = always_429
        try:
            fetch_arxiv.http_get("http://x", "ua")
            raised = False
        except RuntimeError:
            raised = True
        check("全滅なら RuntimeError", raised, True)
        check("既定回数だけ試す", calls["n"], fetch_arxiv.RETRY_COUNT)
        check("最後の失敗後は待たない", len(waits), fetch_arxiv.RETRY_COUNT - 1)
    finally:
        fetch_arxiv._urlopen, fetch_arxiv._sleep = orig_urlopen, orig_sleep


# ---------------------------------------------------------------- collect

ATOM_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/{pid}v1</id>
    <title>{title}</title>
    <summary>{summary}</summary>
    <published>2026-08-12T00:00:00Z</published>
    <updated>2026-08-12T00:00:00Z</updated>
    <author><name>Test Author</name></author>
  </entry>
</feed>
"""

CFG = {
    "defaults": {
        "api_endpoint": "https://export.arxiv.org/api/query",
        "rss_endpoint": "https://rss.arxiv.org/rss/",
        "request_delay_seconds": 0.0,
        "user_agent": "selftest",
        "lookback_days": 2,
        "max_results_per_bucket": 10,
        "keep_top_per_bucket": 5,
        "min_prescore": 1,
    },
    "buckets": [
        {"id": "good", "label": "生きているバケツ",
         "categories": ["cs.AI"], "keywords": ["agent memory"]},
        {"id": "bad", "label": "落ちるバケツ",
         "categories": ["cs.CL"], "keywords": ["survey simulation"]},
    ],
}


def make_args(**over) -> argparse.Namespace:
    base = dict(days=2, bucket=None, max_results=None, keep_top=None,
                min_prescore=None, include_rss=False, new_only=False,
                ignore_seen=True)
    base.update(over)
    return argparse.Namespace(**base)


def test_collect_isolation() -> None:
    print("=== バケツ単位の隔離 ===")
    orig_http_get, orig_sleep = fetch_arxiv.http_get, fetch_arxiv._sleep
    fetch_arxiv._sleep = lambda s: None
    try:
        def flaky(url, ua):
            if "survey+simulation" in url:
                raise RuntimeError("取得に失敗しました: ... (HTTP Error 429)")
            return ATOM_TEMPLATE.format(
                pid="2608.00001", title="On Agent Memory",
                summary="We study agent memory for LLM agents.").encode()

        fetch_arxiv.http_get = flaky
        rows, meta = fetch_arxiv.collect(CFG, make_args())
        check("生きているバケツの候補は残る",
              [r["id"] for r in rows], ["2608.00001"])
        check("失敗したバケツが記録される",
              meta["failed_buckets"], ["落ちるバケツ"])
        check("失敗が理由として表に出る",
              "落ちるバケツ" in meta.get("reason", ""), True)

        def dead(url, ua):
            raise RuntimeError("取得に失敗しました: ... (HTTP Error 429)")

        fetch_arxiv.http_get = dead
        try:
            fetch_arxiv.collect(CFG, make_args())
            raised = False
        except RuntimeError:
            raised = True
        check("全バケツ失敗なら例外", raised, True)
    finally:
        fetch_arxiv.http_get, fetch_arxiv._sleep = orig_http_get, orig_sleep


def main() -> int:
    test_retry_wait()
    test_http_get()
    test_collect_isolation()

    print()
    if failures:
        print(f"失敗 {len(failures)} 件: {', '.join(failures)}")
        return 1
    print("すべて通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
