#!/usr/bin/env python3
"""ACM の新着を Crossref 経由で取得する（arXiv フィードの補完）。

ACM Digital Library には公開APIがない。APIキーは発行できず、機関認証か
Web UI のみで、スクレイピングはIPブロックされる。会員資格でAPIは開かない。

一方 ACM は全論文の DOI を Crossref に登録している。Crossref REST API は
無認証・無料で、DOI接頭辞でフィルタできる。ACM の接頭辞は 10.1145。
本文ではなくメタデータだが、トリアージに必要なのはメタデータだけである。
（2026年1月から ACM DL は全編オープンアクセスなので、本文も購読不要）

出力は fetch_arxiv.py と同じ candidates 形式なので、同じ評価基準で
そのままトリアージにかけられる。

  python3 scripts/fetch_crossref.py --days 7 --mail you@example.com
  python3 scripts/fetch_crossref.py --days 30 --bucket harness --mail you@example.com
  python3 scripts/fetch_crossref.py --days 7 --venue "ICSE" --mail you@example.com

--mail は必須に近い。Crossref は連絡先付きのリクエストを「礼儀正しい利用者」用の
サーバプールに回すので、付けないと不安定になる。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_arxiv as fa  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ACM_PREFIX = "10.1145"
CROSSREF = "https://api.crossref.org/works"
OUT_DIR = ROOT / "out"
SEEN_PATH = ROOT / "state" / "seen_acm.json"

TAG_RE = re.compile(r"<[^>]+>")


def strip_jats(text: str) -> str:
    """Crossref の要旨は JATS 断片で返ることがあるのでタグを落とす。"""
    if not text:
        return ""
    text = TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).replace("Abstract ", "", 1).strip()


def build_url(prefix: str, days: int, mail: str, rows: int, cursor: str, venue: str | None) -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    params = {
        "filter": f"prefix:{prefix},from-created-date:{since}",
        "rows": rows,
        "cursor": cursor,
        "select": "DOI,title,abstract,author,created,container-title,type,URL,subject",
    }
    if mail:
        params["mailto"] = mail
    if venue:
        params["query.container-title"] = venue
    return f"{CROSSREF}?{urllib.parse.urlencode(params)}"


def resolve_publishers(cfg: dict, requested: list[str] | None) -> list[tuple[str, str, str]]:
    """(key, prefix, label) を返す。指定がなければ enabled のものすべて。"""
    pubs = cfg.get("sources", {}).get("publishers", {})
    if not pubs:
        return [("acm", ACM_PREFIX, "ACM")]
    if requested:
        unknown = [k for k in requested if k not in pubs]
        if unknown:
            raise SystemExit(
                f"未知の出版社: {', '.join(unknown)} / 選択肢: {', '.join(pubs)}"
            )
        keys = requested
    else:
        keys = [k for k, v in pubs.items() if v.get("enabled")]
    return [(k, pubs[k]["prefix"], pubs[k].get("label", k)) for k in keys]


def to_entry(item: dict) -> dict | None:
    doi = item.get("DOI")
    title_list = item.get("title") or []
    if not doi or not title_list:
        return None
    created = (item.get("created") or {}).get("date-time", "")[:10]
    venue = (item.get("container-title") or [""])[0]
    return {
        "id": doi,
        "title": fa.clean(title_list[0]),
        "abstract": strip_jats(item.get("abstract", "")),
        "authors": [
            fa.clean(f"{a.get('given', '')} {a.get('family', '')}")
            for a in (item.get("author") or [])
        ][:8],
        "published": created,
        "updated": "",
        "primary_category": item.get("type", ""),
        "categories": [c for c in (item.get("subject") or [])][:4] or [item.get("type", "")],
        "url": item.get("URL") or f"https://doi.org/{doi}",
        "announce_type": "new",
        "source": "crossref",
        "venue": venue,
    }


def collect(cfg: dict, args) -> tuple[list[dict], dict]:
    seen = set() if args.ignore_seen else set(load_seen()["ids"])
    buckets = [b for b in cfg["buckets"] if not args.bucket or b["id"] in args.bucket]
    if not buckets:
        raise SystemExit(f"該当するバケツがありません: {args.bucket}")

    by_id: dict[str, dict] = {}
    meta = {"fetched": 0, "dropped_seen": 0, "dropped_lowscore": 0, "no_abstract": 0,
            "per_publisher": {}}
    publishers = resolve_publishers(cfg, args.publisher)

    for pkey, prefix, plabel in publishers:
        cursor, pages, found = "*", 0, 0
        while pages < args.max_pages:
            url = build_url(prefix, args.days, args.mail, args.rows, cursor, args.venue)
            fa.log(f"{plabel}: page {pages + 1}")
            payload = json.loads(fa.http_get(url, args.user_agent).decode("utf-8"))
            msg = payload.get("message", {})
            items = msg.get("items", [])
            if not items:
                break

            for raw in items:
                e = to_entry(raw)
                if not e:
                    continue
                e["publisher"] = plabel
                meta["fetched"] += 1
                found += 1
                if not e["abstract"]:
                    meta["no_abstract"] += 1
                if e["id"] in seen:
                    meta["dropped_seen"] += 1
                    continue

                for bucket in buckets:
                    score, matched, excluded = fa.prescore(
                        e, bucket["keywords"], bucket.get("exclude_keywords")
                    )
                    if not matched:
                        continue
                    existing = by_id.get(e["id"])
                    if existing:
                        if score > existing["prescore"]:
                            existing["secondary_buckets"].append(existing["bucket_label"])
                            existing.update(
                                bucket_id=bucket["id"],
                                bucket_label=bucket["label"],
                                prescore=score,
                                matched_keywords=matched,
                                excluded_keywords=excluded,
                            )
                        elif bucket["label"] not in existing["secondary_buckets"]:
                            existing["secondary_buckets"].append(bucket["label"])
                    else:
                        row = dict(e)
                        row.update(
                            bucket_id=bucket["id"],
                            bucket_label=bucket["label"],
                            secondary_buckets=[],
                            prescore=score,
                            matched_keywords=matched,
                            excluded_keywords=excluded,
                        )
                        by_id[e["id"]] = row

            cursor = msg.get("next-cursor") or ""
            pages += 1
            if not cursor:
                break
            time.sleep(args.delay)
        meta["per_publisher"][plabel] = found
        time.sleep(args.delay)

    min_score = cfg["defaults"]["min_prescore"] if args.min_prescore is None else args.min_prescore
    cap = args.keep_top or cfg["defaults"]["keep_top_per_bucket"]
    final: list[dict] = []
    for b in buckets:
        rows = sorted(
            (e for e in by_id.values() if e["bucket_id"] == b["id"] and e["prescore"] >= min_score),
            key=lambda x: (-x["prescore"], x["published"]),
        )
        final += rows[:cap]
    meta["dropped_lowscore"] = len(by_id) - len(final)
    meta["candidates"] = len(final)
    meta["window"] = "直近{}日 / {}".format(
        args.days, ", ".join(l for _, _, l in publishers)
    )
    return final, meta


def load_seen() -> dict:
    if SEEN_PATH.exists():
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    return {"ids": [], "last_run": None}


def mark_seen() -> None:
    path = OUT_DIR / "candidates_acm.jsonl"
    if not path.exists():
        raise SystemExit("out/candidates_acm.jsonl がありません。先に取得してください。")
    ids = [json.loads(l)["id"] for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    state = load_seen()
    state["ids"] = sorted(set(state["ids"]) | set(ids))
    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    fa.log(f"既出登録: +{len(ids)} 件 / 累計 {len(state['ids'])} 件")


def write_outputs(rows: list[dict], meta: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "candidates_acm.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    lines = ["# ACM トリアージ候補 (Crossref経由)",
             f"{meta['window']} / 候補 {meta['candidates']} 件", ""]
    for label in dict.fromkeys(r["bucket_label"] for r in rows):
        lines.append(f"## {label}")
        for r in (x for x in rows if x["bucket_label"] == label):
            lines += [
                f"- **{r['title']}** ({r['published']}, 予備{r['prescore']})",
                f"  - {r['publisher']} / {r['venue']} / {r['url']}",
                f"  - 一致: {', '.join(r['matched_keywords'][:6])}",
                f"  - {r['abstract'][:400] or '(Crossrefに要旨なし。DOIを開いて確認する)'}",
            ]
        lines.append("")
    (OUT_DIR / "candidates_acm.md").write_text("\n".join(lines), encoding="utf-8")

    fa.log(
        "取得{fetched} / 要旨なし{no_abstract} / 既出{dropped_seen} / "
        "低スコア{dropped_lowscore} → 候補{candidates}".format(**meta)
    )
    fa.log("出版社別取得数: " + ", ".join(f"{k}={v}" for k, v in meta["per_publisher"].items()))
    if meta["no_abstract"]:
        fa.log(
            f"注意: {meta['no_abstract']}件はCrossrefに要旨がない。"
            "ACMは要旨を必ず登録するわけではないので、タイトルだけで拾えたものは取り逃しが出る。"
        )


def main() -> None:
    p = argparse.ArgumentParser(description="ACM 新着取得 (Crossref)")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--bucket", nargs="*", default=None)
    p.add_argument("--venue", default=None, help="会議・誌名で絞る（例: ICSE, CHI）")
    p.add_argument("--publisher", nargs="*", default=None,
                   help="出版社キー（acm pvldb ieee acl springer）。既定は config で enabled のもの")
    p.add_argument("--mail", default="", help="Crossrefの礼儀正しいプール用の連絡先")
    p.add_argument("--rows", type=int, default=200)
    p.add_argument("--max-pages", type=int, default=10)
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--keep-top", type=int, default=None)
    p.add_argument("--min-prescore", type=int, default=None)
    p.add_argument("--ignore-seen", action="store_true")
    p.add_argument("--mark-seen", action="store_true")
    p.add_argument("--user-agent", default="arxiv-triage/1.0 (ACM via Crossref)")
    p.add_argument("--config", default=str(ROOT / "config" / "queries.json"))
    args = p.parse_args()

    if args.mark_seen:
        mark_seen()
        return
    if not args.mail:
        fa.log("警告: --mail 未指定。Crossrefの共有プールに回されるので不安定になる")

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    rows, meta = collect(cfg, args)
    write_outputs(rows, meta)


if __name__ == "__main__":
    main()
