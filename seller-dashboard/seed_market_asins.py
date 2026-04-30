"""_bestsellers_asins.json から ASIN を market_bsr_meta に登録（初期化用）。

カテゴリ「デジタルカメラ」「交換レンズ」を順位順に取り込み、
bsr_updated_at は NULL のまま（polling 側でラウンドロビン取得される）。

実行: python seed_market_asins.py
"""
import json
import sys
import io
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from db import get_db, init_db

JSON_PATH = Path(__file__).resolve().parent / "_bestsellers_asins.json"
TARGET_CATEGORIES = ("デジタルカメラ", "カメラ用交換レンズ")


def main():
    init_db()
    if not JSON_PATH.exists():
        print(f"NOT FOUND: {JSON_PATH}")
        return
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    items = data.get("items") or []
    # カテゴリでフィルタ + 重複排除（先勝ち = 順位優先）
    seen = set()
    rows = []
    cat_rank = {}  # category -> running rank
    for it in items:
        cat = it.get("category") or ""
        asin = (it.get("asin") or "").strip()
        if not asin:
            continue
        if cat not in TARGET_CATEGORIES:
            continue
        if asin in seen:
            continue
        seen.add(asin)
        cat_rank[cat] = cat_rank.get(cat, 0) + 1
        rows.append((asin, cat, cat_rank[cat]))

    inserted = 0
    skipped = 0
    with get_db() as conn:
        for asin, cat, rank in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO market_bsr_meta(asin, category, rank_in_category) "
                "VALUES(?, ?, ?)",
                (asin, cat, rank),
            )
            if cur.rowcount:
                inserted += 1
            else:
                # 既存があれば rank/category を更新（順位は最新を反映）
                conn.execute(
                    "UPDATE market_bsr_meta SET category=?, rank_in_category=? WHERE asin=?",
                    (cat, rank, asin),
                )
                skipped += 1

    by_cat = {}
    for _, c, _r in rows:
        by_cat[c] = by_cat.get(c, 0) + 1
    print(f"Total source items: {len(items)}")
    print(f"Filtered ASINs: {len(rows)} (by category: {by_cat})")
    print(f"Inserted: {inserted}, Updated: {skipped}")


if __name__ == "__main__":
    main()
