"""市場活況度スコア計算（自社在庫 / カメラ市場 共通）

入力: bsr_history_json のリスト（各要素は [{date:'YYYY-MM-DD', rank:int}, ...] のJSON文字列 or list）
出力: {ym -> {score, median_bsr, raw, asin_count}}

スコア式:
  raw  = max(0, 100 - 10 * log10(BSR中央値))
  score = 過去5年の raw を min-max 正規化して 10〜90 に再スケール
"""
import json
import math
from datetime import date


def compute_market_score(bsr_sources):
    """
    bsr_sources: iterable of (json string | list[dict] | None)
    Returns: dict ym -> {"score": float|None, "median_bsr": int, "raw": float, "asin_count": int}
    """
    market_by_ym = {}  # ym -> [median_bsr_per_asin, ...]
    for src in bsr_sources:
        if not src:
            continue
        if isinstance(src, str):
            try:
                hist = json.loads(src)
            except Exception:
                continue
        else:
            hist = src
        if not hist:
            continue
        asin_by_ym = {}
        for h in hist:
            if not isinstance(h, dict):
                continue
            d = h.get("date") or ""
            r = h.get("rank")
            if not d or not r or r <= 0:
                continue
            ym = d[:7]
            asin_by_ym.setdefault(ym, []).append(r)
        for ym, ranks in asin_by_ym.items():
            if not ranks:
                continue
            med = sorted(ranks)[len(ranks) // 2]
            market_by_ym.setdefault(ym, []).append(med)

    ym_score = {}
    for ym, meds in market_by_ym.items():
        if not meds:
            continue
        s = sorted(meds)
        gmed = s[len(s) // 2]
        raw = max(0, 100 - 10 * math.log10(max(1, gmed)))
        ym_score[ym] = {"raw": raw, "median_bsr": gmed, "asin_count": len(meds)}

    # min-max 正規化（過去5年=直近60ヶ月）
    today = date.today()
    cutoff = (today.year - 5, today.month)
    recent = [v["raw"] for k, v in ym_score.items()
              if (int(k[:4]), int(k[5:7])) >= cutoff]
    if recent and len(recent) >= 2:
        rmin, rmax = min(recent), max(recent)
        span = max(0.01, rmax - rmin)
        for ym, v in ym_score.items():
            scaled = 10 + (v["raw"] - rmin) / span * 80
            v["score"] = round(max(0, min(100, scaled)), 1)
    else:
        for ym, v in ym_score.items():
            v["score"] = round(v["raw"], 1)
    return ym_score
