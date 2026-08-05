#!/usr/bin/env python3
"""arXiv の新着を取得し、既出を差分除外して「トリアージ候補リスト」を作る。

このスクリプトは判断をしない。決定論的な I/O だけを担当する:
  取得 → 重複排除 → 既出除外(差分) → キーワード予備スコア → 候補ファイル出力

スコアリング（S/A/B/C 評価）は SKILL.md の手順に従って Claude 側が行う。
標準ライブラリのみで動作する（pip install 不要）。

使い方:
  python3 scripts/fetch_arxiv.py                        # 全バケツ、設定の既定日数
  python3 scripts/fetch_arxiv.py --days 7 --bucket psych creative
  python3 scripts/fetch_arxiv.py --include-rss          # カテゴリRSSも併用(再現率重視)
  python3 scripts/fetch_arxiv.py --daily                # 無人日次実行（曜日で窓を決める）
  python3 scripts/fetch_arxiv.py --mark-seen            # 配信成功後に既出登録
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from state import (
    RunLedger,
    SeenStore,
    data_home,
    emit_github_output,
    resolve_daily_window,
    today_in,
)

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"

ROOT = Path(__file__).resolve().parent.parent  # スキル本体（設定・採点基準）
HOME = data_home(ROOT)  # 状態と中間ファイル（ARXIV_TRIAGE_HOME で移せる）

CONFIG_PATH = ROOT / "config" / "queries.json"
SEEN_PATH = HOME / "state" / "seen.json"
RUNS_PATH = HOME / "state" / "runs.json"
OUT_DIR = HOME / "out"

ID_RE = re.compile(r"(\d{4}\.\d{4,5})")


# ---------------------------------------------------------------- utilities

def log(msg: str) -> None:
    print(f"[fetch_arxiv] {msg}", file=sys.stderr)


def base_id(raw: str) -> str:
    """2607.27201v1 や oai:arXiv.org:2607.27201 から 2607.27201 を取り出す。"""
    m = ID_RE.search(raw or "")
    return m.group(1) if m else (raw or "").strip()


def http_get(url: str, user_agent: str, retries: int = 3) -> bytes:
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last = exc
            wait = 3.0 * (attempt + 1)
            log(f"取得失敗 ({exc}) — {wait:.0f}秒後に再試行")
            time.sleep(wait)
    raise RuntimeError(f"取得に失敗しました: {url} ({last})")


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


# ---------------------------------------------------------------- query build

def build_api_query(bucket: dict) -> str:
    cats = " OR ".join(f"cat:{c}" for c in bucket["categories"])
    kws = " OR ".join(f'abs:"{k}"' for k in bucket["keywords"])
    return f"({cats}) AND ({kws})"


def api_url(endpoint: str, query: str, max_results: int) -> str:
    params = {
        "search_query": query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    return f"{endpoint}?{urllib.parse.urlencode(params)}"


# ---------------------------------------------------------------- parsers

def parse_atom(payload: bytes) -> list[dict]:
    root = ET.fromstring(payload)
    out = []
    for e in root.findall(f"{ATOM}entry"):
        raw_id = clean(e.findtext(f"{ATOM}id"))
        pid = base_id(raw_id)
        if not pid:
            continue
        prim = e.find(f"{ARXIV_NS}primary_category")
        out.append(
            {
                "id": pid,
                "title": clean(e.findtext(f"{ATOM}title")),
                "abstract": clean(e.findtext(f"{ATOM}summary")),
                "authors": [
                    clean(a.findtext(f"{ATOM}name"))
                    for a in e.findall(f"{ATOM}author")
                ][:8],
                "published": clean(e.findtext(f"{ATOM}published"))[:10],
                "updated": clean(e.findtext(f"{ATOM}updated"))[:10],
                "primary_category": prim.get("term") if prim is not None else "",
                "categories": [
                    c.get("term") for c in e.findall(f"{ATOM}category") if c.get("term")
                ],
                "url": f"https://arxiv.org/abs/{pid}",
                "announce_type": "new",
                "source": "api",
            }
        )
    return out


def parse_rss(payload: bytes) -> list[dict]:
    """rss.arxiv.org のフィードを解析する。announce_type を保持する点が重要。"""
    root = ET.fromstring(payload)
    out = []
    for item in root.iter("item"):
        link = clean(item.findtext("link"))
        guid = clean(item.findtext("guid"))
        pid = base_id(link) or base_id(guid)
        if not pid:
            continue
        desc = clean(item.findtext("description"))
        abstract = desc.split("Abstract:", 1)[1].strip() if "Abstract:" in desc else desc
        out.append(
            {
                "id": pid,
                "title": clean(item.findtext("title")),
                "abstract": abstract,
                "authors": [
                    a.strip()
                    for a in clean(item.findtext(f"{DC_NS}creator")).split(",")
                    if a.strip()
                ][:8],
                "published": clean(item.findtext("pubDate"))[:16],
                "updated": "",
                "primary_category": "",
                "categories": [clean(c.text) for c in item.findall("category") if c.text],
                "url": f"https://arxiv.org/abs/{pid}",
                "announce_type": clean(item.findtext(f"{ARXIV_NS}announce_type")) or "new",
                "source": "rss",
            }
        )
    return out


# ---------------------------------------------------------------- scoring

def prescore(
    entry: dict, keywords: list[str], exclude: list[str] | None = None
) -> tuple[int, list[str], list[str]]:
    """安いキーワード一致による予備スコア。タイトル一致は2点、要旨一致は1点。

    exclude_keywords に一致すると1件あたり-2点。ハードな除外にはしない（LLM が
    最終判断できるよう候補には残す）。LLM に渡す件数を絞るためだけのもので、
    採否の判断はしない。
    """
    title = entry["title"].lower()
    abstract = entry["abstract"].lower()
    score, matched, hits = 0, [], []
    for kw in keywords:
        k = kw.lower()
        # 語数で重み付けする。"multi-agent" のような汎用語より
        # "tool call interception" のような具体語を高く評価し、
        # 汎用語だけを並べたバケツが具体バケツから論文を奪うのを防ぐ。
        w = len(k.replace("-", " ").split())
        if k in title:
            score += 2 * w
            matched.append(kw)
        elif k in abstract:
            score += w
            matched.append(kw)
    for kw in exclude or []:
        if kw.lower() in title or kw.lower() in abstract:
            score -= 4
            hits.append(kw)
    return score, matched, hits


# ---------------------------------------------------------------- main

def collect(cfg: dict, args) -> tuple[list[dict], dict]:
    d = cfg["defaults"]
    ua = d["user_agent"]
    delay = float(d["request_delay_seconds"])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).date()

    buckets = [
        b for b in cfg["buckets"] if not args.bucket or b["id"] in args.bucket
    ]
    if not buckets:
        raise SystemExit(f"該当するバケツがありません: {args.bucket}")

    seen = set() if args.ignore_seen else SeenStore(SEEN_PATH).ids
    by_id: dict[str, dict] = {}
    meta = {"fetched": 0, "dropped_old": 0, "dropped_seen": 0, "dropped_lowscore": 0}

    for i, bucket in enumerate(buckets):
        urls = [api_url(d["api_endpoint"], build_api_query(bucket),
                        args.max_results or d["max_results_per_bucket"])]
        if args.include_rss and bucket.get("rss_feed"):
            urls.append(d["rss_endpoint"] + bucket["rss_feed"])

        entries: list[dict] = []
        for j, url in enumerate(urls):
            if i or j:
                time.sleep(delay)  # arXiv の利用規約: 3秒に1リクエスト
            log(f"{bucket['label']}: {url[:110]}...")
            payload = http_get(url, ua)
            entries += parse_rss(payload) if "rss.arxiv.org" in url else parse_atom(payload)

        for e in entries:
            meta["fetched"] += 1

            if e["source"] == "api":
                try:
                    if datetime.strptime(e["published"], "%Y-%m-%d").date() < cutoff:
                        meta["dropped_old"] += 1
                        continue
                except ValueError:
                    pass

            if args.new_only and e["announce_type"] not in ("new", "cross"):
                meta["dropped_old"] += 1
                continue

            if e["id"] in seen:
                meta["dropped_seen"] += 1
                continue

            score, matched, excluded = prescore(
                e, bucket["keywords"], bucket.get("exclude_keywords")
            )

            # 正の一致が1つも無いバケツは、その論文を主バケツとして主張できない。
            # （減点だけを免れたバケツに論文が流れるのを防ぐ）
            if not matched:
                meta["dropped_lowscore"] += 1
                continue

            existing = by_id.get(e["id"])
            if existing:
                # 複数バケツにまたがる論文は主バケツ1つ + 副タグで持つ（差分化ルール）
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
                continue

            e.update(
                bucket_id=bucket["id"],
                bucket_label=bucket["label"],
                secondary_buckets=[],
                prescore=score,
                matched_keywords=matched,
                excluded_keywords=excluded,
            )
            by_id[e["id"]] = e

    min_score = d["min_prescore"] if args.min_prescore is None else args.min_prescore
    kept: list[dict] = []
    for e in by_id.values():
        if e["prescore"] < min_score:
            meta["dropped_lowscore"] += 1
        else:
            kept.append(e)

    # バケツごとに予備スコア上位のみ残す
    cap = args.keep_top or d["keep_top_per_bucket"]
    final: list[dict] = []
    for b in buckets:
        rows = sorted(
            (e for e in kept if e["bucket_id"] == b["id"]),
            key=lambda x: (-x["prescore"], x["published"]),
        )
        final += rows[:cap]
        meta["dropped_lowscore"] += max(0, len(rows) - cap)

    meta["candidates"] = len(final)
    meta["window"] = f"{cutoff.isoformat()} 〜 {datetime.now(timezone.utc).date().isoformat()}"
    return final, meta


def write_outputs(rows: list[dict], meta: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with (OUT_DIR / "candidates.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    lines = [
        "# arXiv トリアージ候補",
        f"対象期間: {meta['window']} / 候補 {meta['candidates']} 件",
        "",
    ]
    for label in dict.fromkeys(r["bucket_label"] for r in rows):
        lines.append(f"## {label}")
        for r in (x for x in rows if x["bucket_label"] == label):
            extra = f" / 副: {', '.join(r['secondary_buckets'])}" if r["secondary_buckets"] else ""
            lines += [
                f"- **{r['title']}** ({r['id']}, {r['published']}, 予備{r['prescore']}{extra})",
                f"  - {r['url']} / {', '.join(r['categories'][:4])}",
                f"  - 一致: {', '.join(r['matched_keywords'][:6])}"
                + (f" / 除外語: {', '.join(r['excluded_keywords'])}" if r.get("excluded_keywords") else ""),
                f"  - {r['abstract'][:400]}",
            ]
        lines.append("")
    (OUT_DIR / "candidates.md").write_text("\n".join(lines), encoding="utf-8")

    (OUT_DIR / "run_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(
        "取得{fetched} / 期間外{dropped_old} / 既出{dropped_seen} / "
        "低スコア{dropped_lowscore} → 候補{candidates}".format(**meta)
    )


def mark_seen(cfg: dict) -> None:
    """配信が成功した後に呼ぶ。既出登録と実行日の記録を同時に行う。

    この2つは必ずセットにする。既出登録だけして実行日を記録しないと同じ日に
    二重配信され、実行日だけ記録して既出登録しないと翌日また同じ論文が上がる。
    配信前に呼んではいけない（配信が落ちた日の論文が二度と出てこなくなる）。
    """
    path = OUT_DIR / "candidates.jsonl"
    if not path.exists():
        raise SystemExit("out/candidates.jsonl がありません。先に取得を実行してください。")
    ids = [json.loads(l)["id"] for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    today = today_in(cfg.get("daily_schedule", {}).get("timezone"))
    seen = SeenStore(SEEN_PATH)
    added = seen.add(ids)
    dropped = seen.prune(int(cfg["defaults"].get("seen_retention_months", 4)), today)
    seen.save()
    RunLedger(RUNS_PATH).record(today, candidates=len(ids), newly_seen=added)

    log(f"既出登録: +{added} 件（候補 {len(ids)} 件中）/ 期限切れ削除 {dropped} 件 / 累計 {len(seen)} 件")
    log(f"実行日を記録: {today.isoformat()}")


def report_status(meta: dict, status: str, day) -> None:
    """run_meta.json と CI の step output に結果を書く。"""
    meta = {**meta, "status": status, "date": day.isoformat()}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "run_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    emit_github_output(
        status=status, candidates=meta.get("candidates", 0), date=day.isoformat()
    )


def resolve_daily_run(cfg: dict, args, today) -> bool:
    """無人日次実行の可否と取得窓を決める。走らせないときは False を返す。"""
    should_run, days, reason = resolve_daily_window(cfg.get("daily_schedule", {}), today)
    if not should_run:
        log(reason)
        report_status({"candidates": 0, "reason": reason}, "skipped_weekend", today)
        return False

    if not args.force and RunLedger(RUNS_PATH).completed(today):
        reason = f"{today.isoformat()} は配信済み（再実行するなら --force）"
        log(reason)
        report_status({"candidates": 0, "reason": reason}, "skipped_already_delivered", today)
        return False

    if args.days is None:
        args.days = days
    log(reason)
    return True


def main() -> None:
    p = argparse.ArgumentParser(description="arXiv 新着取得 + 差分抽出")
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument("--days", type=int, default=None, help="遡る日数（既定は設定ファイル）")
    p.add_argument("--bucket", nargs="*", default=None, help="対象バケツID")
    p.add_argument("--max-results", type=int, default=None)
    p.add_argument("--keep-top", type=int, default=None)
    p.add_argument("--min-prescore", type=int, default=None)
    p.add_argument("--include-rss", action="store_true", help="カテゴリRSSも併用する")
    p.add_argument("--new-only", action="store_true", help="RSS の replace 系を除外する")
    p.add_argument("--ignore-seen", action="store_true", help="既出除外をしない")
    p.add_argument("--mark-seen", action="store_true", help="配信成功後に既出登録して終了")
    p.add_argument(
        "--daily",
        action="store_true",
        help="無人日次実行: 曜日で取得窓を決め、土日と配信済みの日はスキップする",
    )
    p.add_argument("--force", action="store_true", help="配信済みの日でも実行する")
    args = p.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))

    if args.mark_seen:
        mark_seen(cfg)
        return

    today = today_in(cfg.get("daily_schedule", {}).get("timezone"))
    if args.daily and not resolve_daily_run(cfg, args, today):
        return  # 終了コードは0のまま。CI を失敗させずに後続ステップだけ止める

    if args.days is None:
        args.days = cfg["defaults"]["lookback_days"]

    rows, meta = collect(cfg, args)
    write_outputs(rows, meta)
    report_status(meta, "ok" if rows else "no_candidates", today)


if __name__ == "__main__":
    main()
