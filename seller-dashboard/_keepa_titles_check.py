"""取得した422 ASINのうち、先頭20件のタイトル・価格を確認"""
import sys, io, json, urllib.request, urllib.parse, gzip
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from db import get_setting

api_key = get_setting("keepa_api_key", "")
with open("_finder_asins.json", encoding="utf-8") as f:
    data = json.load(f)
asins = data["asins"][:20]

url = (f"https://api.keepa.com/product"
       f"?key={api_key}&domain=5&asin={','.join(asins)}&stats=1")
req = urllib.request.Request(url, headers={
    "User-Agent": "seller-dashboard/1.0",
    "Accept-Encoding": "gzip",
})
with urllib.request.urlopen(req, timeout=30) as resp:
    raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    pl = json.loads(raw.decode("utf-8"))

print(f"残トークン: {pl.get('tokensLeft')}\n")
for p in pl.get("products", []):
    title = (p.get("title") or "")[:60]
    cur = p.get("current") or []
    new_price = cur[1] if len(cur) > 1 and cur[1] > 0 else None
    bsr = cur[3] if len(cur) > 3 and cur[3] > 0 else None
    cats = p.get("categoryTree") or []
    cat_top = cats[-1].get("name") if cats else "?"
    yen = f"¥{new_price//100:,}" if new_price else "—"
    print(f"  {p.get('asin')}  BSR={bsr or '—':>8}  {yen:>10}  [{cat_top}]  {title}")
