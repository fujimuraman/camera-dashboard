"""app.py と同じ get_db() で BSR を確認"""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from db import get_db
from config import DB_PATH

print(f"DB_PATH = {DB_PATH}")
with get_db() as conn:
    rows = conn.execute(
        "SELECT seller_sku, status, quantity, "
        "bsr_history_json IS NOT NULL AS has_hist, "
        "length(bsr_history_json) AS hl "
        "FROM inventory WHERE bsr_history_json IS NOT NULL LIMIT 5"
    ).fetchall()
    print(f"sample rows: {len(rows)}")
    for r in rows:
        print(dict(r))

    rows2 = conn.execute(
        "SELECT COUNT(*) AS c FROM inventory "
        "WHERE status LIKE 'Active%' AND quantity > 0 AND bsr_history_json IS NOT NULL"
    ).fetchone()
    print(f"matching active+qty>0+hist: {rows2['c']}")

    rows3 = conn.execute(
        "SELECT COUNT(*) AS c, "
        "SUM(CASE WHEN status LIKE 'Active%' THEN 1 ELSE 0 END) AS active_c, "
        "SUM(CASE WHEN quantity>0 THEN 1 ELSE 0 END) AS qty_c "
        "FROM inventory WHERE bsr_history_json IS NOT NULL"
    ).fetchone()
    print(f"hist total: {dict(rows3)}")
