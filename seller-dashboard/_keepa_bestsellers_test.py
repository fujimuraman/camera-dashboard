"""Keepa Best Sellers API: カメラカテゴリの売れ筋トップを純粋なBSR順で取得"""
import sys, io, json, urllib.request, urllib.parse, gzip
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from db import get_setting

api_key = get_setting("keepa_api_key", "")
if not api_key:
    print("ERROR: keepa_api_key not set"); sys.exit(1)


def fetch_bestsellers(category_id: int, range_param: str = "30") -> dict:
    """指定カテゴリのベストセラー ASIN リストを取得"""
    url = (
        f"https://api.keepa.com/bestsellers"
        f"?key={api_key}&domain=5&category={category_id}&range={range_param}"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "seller-dashboard/1.0",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


# デジタルカメラ + 交換レンズ
combined = []
seen = set()
for cat_id, name in [(3371371, "デジタルカメラ"), (2285023051, "カメラ用交換レンズ")]:
    print(f"\n=== {name} (catId={cat_id}) ===")
    data = fetch_bestsellers(cat_id)
    bs = data.get("bestSellersList") or {}
    asins = bs.get("asinList") or []
    print(f"  ASIN 数: {len(asins)}")
    print(f"  残トークン: {data.get('tokensLeft')}")
    print(f"  先頭5件: {asins[:5]}")
    for a in asins:
        if a not in seen:
            seen.add(a)
            combined.append({"asin": a, "category": name})

print(f"\n=== 合算（重複除外）===")
print(f"  合計 ASIN: {len(combined)}")

with open("_bestsellers_asins.json", "w", encoding="utf-8") as f:
    json.dump({"items": combined}, f, ensure_ascii=False, indent=2)
print("→ _bestsellers_asins.json に保存")
