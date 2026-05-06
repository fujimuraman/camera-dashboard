"""自社仕入れ対象 ASIN（需要 S/A/B）を market_bsr_meta に投入。

データソース: data/target_asins.json
  各要素: {sheet, asin, demand, model, sales, stock, ...}

既存 market_bsr_meta レコードはすべて DELETE してから再投入。
新仕様で必要な列:
  - source = 'target_list'
  - demand_rank = 'S' | 'A' | 'B'
  - category = sheet
  - title = model
  - fetch_attempts = 0

メーカー・カテゴリ偏りを避けるため、投入前にランダムシャッフル。
取得時の順序もランダム化されるので、途中経過のスコアもバランス保つ。

実行: cd C:\\claude\\seller-dashboard && python seed_market_asins.py
"""
import json
import random
import sys
import io
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from db import get_db, init_db

# プロジェクト内のデータファイル（配布リポジトリにも含まれる）
JSON_PATH = Path(__file__).resolve().parent / "data" / "target_asins.json"


def main():
    init_db()
    if not JSON_PATH.exists():
        print(f"NOT FOUND: {JSON_PATH}")
        return
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        print(f"UNEXPECTED FORMAT: top-level should be a list, got {type(data).__name__}")
        return

    # demand が S/A/B のみ採用、ASIN 重複排除（先勝ち）
    seen = set()
    rows = []
    for it in data:
        asin = (it.get("asin") or "").strip()
        demand = (it.get("demand") or "").strip().upper()
        if not asin:
            continue
        if demand not in ("S", "A", "B"):
            continue
        if asin in seen:
            continue
        seen.add(asin)
        rows.append({
            "asin": asin,
            "demand": demand,
            "sheet": (it.get("sheet") or "") or None,
            "model": (it.get("model") or "") or None,
        })

    by_demand = {"S": 0, "A": 0, "B": 0}
    for r in rows:
        by_demand[r["demand"]] = by_demand.get(r["demand"], 0) + 1

    # メーカー・カテゴリ偏り回避のためシャッフル（再現性を持たせるためseed固定）
    random.Random(42).shuffle(rows)

    with get_db() as conn:
        # 既存を全削除（旧 Best Sellers ベースの 8,658件を撤廃）
        before = conn.execute("SELECT COUNT(*) FROM market_bsr_meta").fetchone()[0]
        conn.execute("DELETE FROM market_bsr_meta")

        for r in rows:
            conn.execute(
                "INSERT INTO market_bsr_meta(asin, category, title, demand_rank, "
                "  source, fetch_attempts) "
                "VALUES(?, ?, ?, ?, 'target_list', 0)",
                (r["asin"], r["sheet"], r["model"], r["demand"]),
            )

    print(f"Source items in JSON: {len(data)}")
    print(f"Filtered S/A/B unique ASINs: {len(rows)} (S={by_demand['S']}, A={by_demand['A']}, B={by_demand['B']})")
    print(f"Cleared previous rows: {before}")
    print(f"Inserted: {len(rows)}")


if __name__ == "__main__":
    main()
