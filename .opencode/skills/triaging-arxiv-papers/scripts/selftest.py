#!/usr/bin/env python3
"""キーワード設定が「拾えるべき論文」を実際に拾えるか検証する。

継続抽出の仕組みで最初に腐るのはキーワードである。取り逃していても
静かに0件になるだけで、誰も気づかない。だから既知の良い論文を
フィクスチャとして固定し、設定を変えるたびにこれを回す。

  python3 scripts/selftest.py              # タイトルのみで判定（オフライン・下限値）
  python3 scripts/selftest.py --live       # arXiv から要旨を取って本番同等で判定

--live なしはタイトル一致のみなので**下限**である。ここで落ちても
要旨で拾える可能性は残る。逆に --live で落ちたら本物の取り逃しである。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_arxiv as fa  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from state import data_home  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = data_home(ROOT) / "state" / "fixtures.jsonl"


def load_fixtures() -> list[dict]:
    return [
        json.loads(line)
        for line in FIXTURES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def fetch_abstracts(ids: list[str], cfg: dict) -> dict[str, str]:
    """id_list でまとめて要旨を取得する（1リクエスト）。"""
    d = cfg["defaults"]
    url = (
        f"{d['api_endpoint']}?"
        + urllib.parse.urlencode({"id_list": ",".join(ids), "max_results": len(ids)})
    )
    payload = fa.http_get(url, d["user_agent"])
    return {e["id"]: e["abstract"] for e in fa.parse_atom(payload)}


def score_all(text_by_id: dict[str, str], fixtures: list[dict], cfg: dict) -> list[dict]:
    rows = []
    for fx in fixtures:
        entry = {"title": fx["title"], "abstract": text_by_id.get(fx["id"], "")}
        scores = {}
        for b in cfg["buckets"]:
            s, matched, excl = fa.prescore(
                entry, b["keywords"], b.get("exclude_keywords")
            )
            if matched:
                scores[b["id"]] = (s, matched, excl)
        best = max(scores.items(), key=lambda kv: kv[1][0])[0] if scores else None
        rows.append(
            {
                "id": fx["id"],
                "title": fx["title"],
                "expect": fx["expect"],
                "got": best,
                "score": scores.get(best, (0, [], []))[0] if best else 0,
                "matched": scores.get(fx["expect"], (0, [], []))[1],
                "all": {k: v[0] for k, v in scores.items()},
            }
        )
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description="キーワード設定の回帰テスト")
    p.add_argument("--live", action="store_true", help="arXiv から要旨を取得して判定")
    p.add_argument("--config", default=str(ROOT / "config" / "queries.json"))
    args = p.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    fixtures = load_fixtures()

    text_by_id: dict[str, str] = {}
    if args.live:
        print("arXiv から要旨を取得中...", file=sys.stderr)
        text_by_id = fetch_abstracts([f["id"] for f in fixtures], cfg)
        missing = [f["id"] for f in fixtures if f["id"] not in text_by_id]
        if missing:
            print(f"要旨が取れなかったID: {', '.join(missing)}", file=sys.stderr)
        time.sleep(cfg["defaults"]["request_delay_seconds"])

    rows = score_all(text_by_id, fixtures, cfg)
    mode = "要旨あり(live)" if args.live else "タイトルのみ(下限)"

    miss = [r for r in rows if r["got"] is None]
    wrong = [r for r in rows if r["got"] and r["got"] != r["expect"]]
    ok = [r for r in rows if r["got"] == r["expect"]]

    print(f"\n=== キーワード回帰テスト / {mode} ===")
    print(f"正解 {len(ok)} / 誤バケツ {len(wrong)} / 取り逃し {len(miss)}  (全{len(rows)}件)\n")

    if miss:
        print("■ 取り逃し（どのバケツも正の一致なし＝設定を直すべき対象）")
        for r in miss:
            print(f"  {r['id']}  {r['title'][:70]}")
            print(f"      期待バケツ: {r['expect']}")
        print()

    if wrong:
        print("■ 誤バケツ（拾えるが主バケツが違う。副タグに入るので致命的ではない）")
        for r in wrong:
            print(f"  {r['id']}  {r['title'][:60]}")
            print(f"      期待 {r['expect']} → 実際 {r['got']} / 各バケツ得点 {r['all']}")
        print()

    if ok:
        print("■ 正解")
        for r in ok:
            kw = ", ".join(r["matched"][:4]) or "-"
            print(f"  {r['id']}  得点{r['score']:>3}  一致: {kw}")
        print()

    # 取り逃しがあれば異常終了する。CI やフックから回せるように。
    sys.exit(1 if miss else 0)


if __name__ == "__main__":
    main()
