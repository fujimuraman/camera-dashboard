"""AIS3 の ASIN登録 に渡す、You and Me の出品中 ASIN の一覧を作る（週次の在庫見直し ②）

使い方: python ais3_asin_list.py [今回アップした〇〇deleting.tsv ...]
  - 全出品レポートの Active な出品の ASIN（今回消した SKU は除く）
  - 上限 20万件（2026-10-31 かおる決定）。超えた分は入れず、件数を報告する
  - 書き出し先: ais3_weekly/asins_YYYYMMDD.csv（見出し無し・1行1 ASIN・BOM付き・CRLF。かおるの手作業の形式と同じ）
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from upload_ais3_files import listing_rows, read_rows

LIMIT = 200_000
OUT_DIR = Path(__file__).resolve().parents[1] / "ais3_weekly"


def main():
    deleted = set()
    for p in sys.argv[1:]:
        deleted |= {r["contribution_sku#1.value"] for r in read_rows(Path(p))}
    rows = [r for r in listing_rows() if r["ステータス"] == "Active" and r["出品者SKU"] not in deleted]
    asins = sorted({r["商品ID"] for r in rows if r["商品ID"]})
    cut = asins[LIMIT:]
    asins = asins[:LIMIT]
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"asins_{datetime.now():%Y%m%d}.csv"
    out.write_bytes(("﻿" + "\r\n".join(asins) + "\r\n").encode("utf-8"))
    print(f"出品中 ASIN {len(asins) + len(cut)}件（今回の削除 {len(deleted)}件は除外済み）→ {out}")
    print(f"登録する {len(asins)}件" + (f" / 上限超えで今回は見直さない {len(cut)}件（{cut[0]}〜{cut[-1]}）" if cut else ""))


if __name__ == "__main__":
    main()
