"""analytics 経由で monthly データの market_score 確認"""
import sys, io, sqlite3, json, math
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

con = sqlite3.connect('data.db')
con.row_factory = sqlite3.Row
cur = con.cursor()

# analytics と同じ集計を再現
bsr_rows = list(cur.execute(
    "SELECT bsr_history_json FROM inventory "
    "WHERE status LIKE 'Active%' AND quantity > 0 AND bsr_history_json IS NOT NULL"
))
print(f"BSR履歴あり SKU: {len(bsr_rows)}")

market_score_by_ym = {}
for r in bsr_rows:
    try:
        hist = json.loads(r["bsr_history_json"] or "[]")
    except:
        continue
    asin_by_ym = {}
    for h in hist:
        d = h.get("date") or ""
        rank = h.get("rank")
        if not d or not rank or rank <= 0:
            continue
        ym = d[:7]
        asin_by_ym.setdefault(ym, []).append(rank)
    for ym, ranks in asin_by_ym.items():
        if not ranks:
            continue
        med = sorted(ranks)[len(ranks)//2]
        market_score_by_ym.setdefault(ym, []).append(med)

ym_score = {}
for ym, meds in market_score_by_ym.items():
    if not meds: continue
    s = sorted(meds)
    gm = s[len(s)//2]
    score = max(0, 100 - 10 * math.log10(max(1, gm)))
    ym_score[ym] = (round(score, 1), gm)

print()
print("=== 直近12ヶ月の市場活況度 ===")
for ym in sorted(ym_score.keys())[-12:]:
    sc, bsr = ym_score[ym]
    print(f"  {ym}: score={sc:>5}  BSR中央値={bsr:>8,}")
