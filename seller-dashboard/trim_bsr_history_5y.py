"""bsr_history_json を過去5年分（1825日）に切り詰めるマイグレーション。

スコア計算は過去5年の min/max でしか使わないため、それ以前のデータは保持不要。
- inventory.bsr_history_json
- market_bsr_meta.bsr_history_json

実行: python trim_bsr_history_5y.py
DRY-RUN（書き込まず件数のみ）: python trim_bsr_history_5y.py --dry-run
"""
import sys
import json
import sqlite3
import argparse
from datetime import date, timedelta
from pathlib import Path

# config 経由で DB_PATH 取得
sys.path.insert(0, str(Path(__file__).parent))
from config import DB_PATH


def trim(dry_run: bool) -> None:
    cutoff_iso = (date.today() - timedelta(days=1825)).isoformat()
    print(f"カットオフ: {cutoff_iso} 以前を削除")
    print(f"DB: {DB_PATH}")
    print()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    for table in ["inventory", "market_bsr_meta"]:
        rows = conn.execute(
            f"SELECT rowid, asin, bsr_history_json FROM {table} "
            f"WHERE bsr_history_json IS NOT NULL AND bsr_history_json != '[]'"
        ).fetchall()
        total_before = 0
        total_after = 0
        bytes_before = 0
        bytes_after = 0
        updates = []
        for r in rows:
            try:
                hist = json.loads(r["bsr_history_json"] or "[]")
            except Exception:
                continue
            before_n = len(hist)
            trimmed = [h for h in hist if h.get("date", "") >= cutoff_iso]
            after_n = len(trimmed)
            if after_n == before_n:
                continue  # 切るものなし
            new_json = json.dumps(trimmed, ensure_ascii=False)
            total_before += before_n
            total_after += after_n
            bytes_before += len(r["bsr_history_json"] or "")
            bytes_after += len(new_json)
            updates.append((new_json, r["rowid"]))
        print(f"--- {table} ---")
        print(f"  対象行: {len(rows)} / 切り詰め対象: {len(updates)}")
        print(f"  ポイント数: {total_before:,} → {total_after:,} ({total_before - total_after:,}削減)")
        print(f"  サイズ: {bytes_before/1024/1024:.1f}MB → {bytes_after/1024/1024:.1f}MB "
              f"({(bytes_before-bytes_after)/1024/1024:.1f}MB削減)")
        if not dry_run and updates:
            conn.executemany(
                f"UPDATE {table} SET bsr_history_json = ? WHERE rowid = ?",
                updates,
            )
            conn.commit()
            print(f"  [OK] 書き込み完了")
        elif dry_run:
            print(f"  [DRY-RUN] 書き込みスキップ")
        print()

    if not dry_run:
        # VACUUM で実ファイルサイズも縮める
        print("VACUUM 実行中...")
        conn.execute("VACUUM")
        print("[OK] VACUUM 完了")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="書き込まず件数のみ表示")
    args = ap.parse_args()
    trim(args.dry_run)
