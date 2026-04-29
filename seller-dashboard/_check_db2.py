import sys, io, json, math
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from db import get_db

with get_db() as conn:
    rows = conn.execute(
        "SELECT bsr_history_json FROM inventory "
        "WHERE status LIKE 'Active%' AND quantity > 0 AND bsr_history_json IS NOT NULL"
    ).fetchall()
    print(f"rows: {len(rows)}")
    market_score_by_ym = {}
    for r in rows:
        try:
            hist = json.loads(r["bsr_history_json"] or "[]")
        except:
            continue
        asin_by_ym = {}
        for h in hist:
            d = h.get("date") or ""
            rk = h.get("rank")
            if not d or not rk or rk <= 0: continue
            ym = d[:7]
            asin_by_ym.setdefault(ym, []).append(rk)
        for ym, ranks in asin_by_ym.items():
            med = sorted(ranks)[len(ranks)//2]
            market_score_by_ym.setdefault(ym, []).append(med)
    print(f"ym keys: {sorted(market_score_by_ym.keys())[-6:]}")
    ym_score = {}
    for ym, meds in market_score_by_ym.items():
        s = sorted(meds)
        gm = s[len(s)//2]
        ym_score[ym] = round(max(0, 100 - 10*math.log10(max(1,gm))), 1)
    print(f"recent: {[(k, ym_score[k]) for k in sorted(ym_score.keys())[-6:]]}")
