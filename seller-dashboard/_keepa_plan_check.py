"""Keepa プラン状況確認: 残トークン・回復速度・契約タイプ"""
import sys, io, json, urllib.request, gzip
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from db import get_setting

api_key = get_setting("keepa_api_key", "")
url = f"https://api.keepa.com/token?key={api_key}"
req = urllib.request.Request(url, headers={"Accept-Encoding":"gzip"})
with urllib.request.urlopen(req, timeout=15) as resp:
    raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw.decode("utf-8"))
print(json.dumps(data, indent=2, ensure_ascii=False))
