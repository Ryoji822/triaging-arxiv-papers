#!/usr/bin/env python3
"""既出IDと実行履歴の永続化層、および日次スケジュールの解決。

日次の無人実行で重複が起きる経路は4つあり、それぞれ別の仕組みで止める:

  1. 同じ論文が翌日の取得窓にも入る     → SeenStore（arXiv ID / DOI 単位の除外）
  2. ワークフローが同じ日に再実行される → RunLedger（配信成功した日付を記録）
  3. 書き込み中に落ちて状態が壊れる     → write_json_atomic（一時ファイル + os.replace）
  4. 状態ファイルが無限に育つ           → SeenStore.prune（arXiv ID の年月で刈る）

重要な設計判断: 実行履歴に記録するのは「配信まで成功した日」だけである。
取得だけして落ちた日を記録してしまうと、その日の論文が二度と出てこない。
重複を防ぐことと取り逃しを防ぐことは両立させなければならない。

標準ライブラリのみで動作する（pip install 不要）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - 3.8 以前でも落とさない
    ZoneInfo = None  # type: ignore[assignment]

# arXiv ID の年月部分。2608.01679 → 2608（2026年8月）
YYMM_RE = re.compile(r"^(\d{2})(\d{2})\.\d{4,5}$")

RUN_HISTORY_LIMIT = 180  # 実行履歴の保持件数（半年強）

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def log(msg: str) -> None:
    print(f"[state] {msg}", file=sys.stderr)


# ---------------------------------------------------------------- primitives

def read_json(path: Path, default: dict) -> dict:
    """壊れた状態ファイルで実行全体を止めない。読めなければ既定値を返す。"""
    if not path.exists():
        return dict(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log(f"警告: {path.name} が壊れています ({exc})。既定値で続行します")
        return dict(default)


def write_json_atomic(path: Path, data: dict) -> None:
    """同じディレクトリに一時ファイルを作ってから置換する。

    途中で落ちても元のファイルは無傷で残る。同一ファイルシステム上でないと
    os.replace が原子的にならないので、一時ファイルは必ず同じ親に作る。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def data_home(skill_root: Path) -> Path:
    """状態ファイルと中間ファイルの置き場所を決める。

    優先順位:
      1. 環境変数 ARXIV_TRIAGE_HOME（明示指定）
      2. リポジトリのルート — スキルが `<root>/.opencode/skills/<name>/` や
         `<root>/.claude/skills/<name>/` の下にあり、かつ `<root>/state/known-topics.md`
         が存在する場合
      3. スキル自身のディレクトリ（`~/.claude/skills/` に単体で置いた場合）

    2が必要な理由: 環境変数の指定を忘れた手動実行が、CI と別の場所に台帳を
    書いてしまうと、状態が2つに分裂して重複除外も二重配信防止も効かなくなる。
    しかもエラーは出ないので静かに壊れる。台帳の実在で判定するので、
    ホームディレクトリを誤ってルートと見なすことはない。
    """
    env = os.environ.get("ARXIV_TRIAGE_HOME")
    if env:
        return Path(env).expanduser().resolve()
    for parent in skill_root.parents:
        if parent.name in (".opencode", ".claude"):
            root = parent.parent
            if (root / "state" / "known-topics.md").exists():
                return root
    return skill_root


def today_in(tz_name: str | None) -> date:
    """設定されたタイムゾーンでの「今日」。日付境界がずれると日次判定が壊れる。"""
    if tz_name and ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name)).date()
        except Exception as exc:  # 未知のTZ名で落とさない
            log(f"警告: タイムゾーン {tz_name} を解決できません ({exc})。UTCを使います")
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------- schedule

def resolve_daily_window(schedule: dict, day: date) -> tuple[bool, int, str]:
    """その日に走らせるべきか、遡る日数、理由を返す。

    arXiv の告知は平日 20:00 ET（= 翌日 09-10時 JST）に出て、土日は出ない。
    告知1回ぶんの投稿日は次のように散る:

      月曜の告知 = 金14:00 ET〜日14:00 ET の投稿 → published は金・土・日
      火〜金の告知 = 前日14:00 ET〜当日14:00 ET  → published は前日・当日

    つまり必要な窓は曜日で変わる。全曜日を一律に広げるのではなく、
    月曜だけ広く取り、土日は走らせない。窓が重なった分は SeenStore が潰す。
    """
    key = WEEKDAY_KEYS[day.weekday()]
    skip = [s.lower() for s in schedule.get("skip_weekdays", [])]
    if key in skip:
        return False, 0, f"{key}: arXiv の告知がない曜日のためスキップ"

    table = schedule.get("lookback_days", {})
    days = int(table.get(key, table.get("default", 2)))
    return True, days, f"{key}: 遡り {days} 日"


# ---------------------------------------------------------------- seen store

class SeenStore:
    """既出ID集合。二度目に上がってきた論文をここで落とす。"""

    def __init__(self, path: Path):
        self.path = path
        raw = read_json(path, {"ids": [], "last_run": None})
        self._ids: set[str] = set(raw.get("ids") or [])
        self._meta = {k: v for k, v in raw.items() if k != "ids"}

    def __contains__(self, paper_id: str) -> bool:
        return paper_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    @property
    def ids(self) -> set[str]:
        return set(self._ids)

    def add(self, ids) -> int:
        """新規に加わった件数を返す（同じIDを二度足しても増えない）。"""
        before = len(self._ids)
        self._ids |= {i for i in ids if i}
        return len(self._ids) - before

    def prune(self, retention_months: int, today: date) -> int:
        """arXiv ID の年月が保持期間より古いものを捨て、捨てた件数を返す。

        取得窓は数日しかないので、数か月前のIDを覚えておく必要はない。
        年月形式に一致しないID（DOI等）は判定できないので残す。
        """
        if retention_months <= 0:
            return 0
        cutoff = _shift_months(today, -retention_months)
        cutoff_key = (cutoff.year % 100, cutoff.month)
        kept, dropped = set(), 0
        for pid in self._ids:
            m = YYMM_RE.match(pid)
            if m and (int(m.group(1)), int(m.group(2))) < cutoff_key:
                dropped += 1
            else:
                kept.add(pid)
        self._ids = kept
        return dropped

    def save(self, run_at: str | None = None) -> None:
        payload = dict(self._meta)
        payload["ids"] = sorted(self._ids)
        payload["last_run"] = run_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_json_atomic(self.path, payload)


def _shift_months(d: date, delta: int) -> date:
    total = (d.year * 12 + d.month - 1) + delta
    return date(total // 12, total % 12 + 1, 1)


# ---------------------------------------------------------------- run ledger

class RunLedger:
    """配信まで成功した日を記録する台帳。同じ日の二重配信を止める。"""

    def __init__(self, path: Path):
        self.path = path
        raw = read_json(path, {"runs": {}})
        self._runs: dict = raw.get("runs") or {}

    def completed(self, day: date) -> bool:
        return day.isoformat() in self._runs

    def entry(self, day: date) -> dict | None:
        return self._runs.get(day.isoformat())

    def record(self, day: date, **payload) -> None:
        self._runs[day.isoformat()] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **payload,
        }
        for old in sorted(self._runs)[:-RUN_HISTORY_LIMIT]:
            del self._runs[old]
        write_json_atomic(self.path, {"runs": self._runs})


# ---------------------------------------------------------------- CI 連携

def emit_github_output(**pairs) -> None:
    """GitHub Actions の step output に書く。ローカルでは何もしない。

    後続ステップが「配信すべきか」を分岐できるようにするためのもので、
    この関数の有無でスクリプトの挙動は変わらない（CI非依存を保つ）。
    """
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as f:
        for key, value in pairs.items():
            f.write(f"{key}={value}\n")
