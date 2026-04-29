"""Keepa Product Finder: カメラ + レンズ AND ¥10,000以上 のトップ500"""
import sys, io, json, urllib.request, urllib.parse, gzip
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from db import get_setting

api_key = get_setting("keepa_api_key", "")
if not api_key:
    print("ERROR: keepa_api_key not set"); sys.exit(1)

selection = {
    "current_NEW_gte": 1000000,       # ¥10,000 (Keepa単位 = 円×100)
    "current_SALES_gte": 1,           # SalesRank が記録されているもののみ（ノイズ除去）
    "current_SALES_lte": 500000,      # 上限（極端な不人気品は除外）
    "categories_include": [3371371, 2285023051],
    "sort": [["current_SALES", "asc"]],  # SalesRank昇順=売れ筋
    "perPage": 500,
    "page": 0,
}
url = (f"https://api.keepa.com/query"
       f"?key={api_key}&domain=5"
       f"&selection={urllib.parse.quote(json.dumps(selection))}")
req = urllib.request.Request(url, headers={
    "User-Agent": "seller-dashboard/1.0",
    "Accept-Encoding": "gzip",
})
with urllib.request.urlopen(req, timeout=60) as resp:
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw.decode("utf-8"))

asins = data.get("asinList") or []
print(f"取得 ASIN 数: {len(asins)}")
print(f"残トークン: {data.get('tokensLeft')}")
print(f"先頭10件: {asins[:10]}")
print(f"末尾5件: {asins[-5:]}")

# データ保存（次のステップで使用）
import json as _j
with open("_finder_asins.json", "w", encoding="utf-8") as f:
    _j.dump({"asins": asins, "fetched_at": data.get("timestamp")}, f, ensure_ascii=False, indent=2)
print("\n→ _finder_asins.json に保存")
