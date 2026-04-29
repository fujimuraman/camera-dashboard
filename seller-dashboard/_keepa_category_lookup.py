"""Keepa Category Lookup: デジタルカメラ + 交換レンズ の categoryId を特定"""
import sys, io, json, urllib.request, urllib.parse, gzip
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from db import get_setting

api_key = get_setting("keepa_api_key", "")
if not api_key:
    print("ERROR: keepa_api_key not set")
    sys.exit(1)


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "seller-dashboard/1.0",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


# 1. キーワード検索でカテゴリ探索
for term in ["デジタルカメラ", "交換レンズ", "カメラ", "レンズ"]:
    url = (f"https://api.keepa.com/search"
           f"?key={api_key}&domain=5&type=category"
           f"&term={urllib.parse.quote(term)}")
    print(f"\n=== term: {term} ===")
    try:
        data = fetch(url)
        cats = data.get("categories", {})
        # categories は dict（catId -> {name, parent, productCount}）
        items = list(cats.values()) if isinstance(cats, dict) else cats
        # 商品数の多い順 上位20
        items.sort(key=lambda x: -(x.get("productCount") or 0))
        for c in items[:15]:
            print(f"  {c.get('catId'):>12}  pc={c.get('productCount'):>8}  "
                  f"parent={c.get('parent')}  name={c.get('name')}")
        print(f"  tokensLeft: {data.get('tokensLeft')}")
    except Exception as e:
        print(f"  ERR: {e}")
