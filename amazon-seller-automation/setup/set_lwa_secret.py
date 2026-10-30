"""クリップボードの LWA client secret を .env の <SHOP>_LWA_CLIENT_SECRET に書き込む（値は表示しない）
使い方: python set_lwa_secret.py FUJI|YANDME
"""
import os, re, shutil, subprocess, sys
from datetime import datetime

ENV = r"C:\claude\amazon-seller-automation\.env"
shop = sys.argv[1].upper()
assert shop in ("FUJI", "YANDME")
key = f"{shop}_LWA_CLIENT_SECRET"

val = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                     capture_output=True, text=True).stdout.strip()
if not val.startswith("amzn1.oa2-cs.v1."):
    sys.exit(f"クリップボードの中身が LWA secret の形（amzn1.oa2-cs.v1.…）ではない。長さ={len(val)}")

text = open(ENV, encoding="utf-8").read()
if not re.search(rf"^{key}=", text, re.M):
    sys.exit(f"{key} が .env に無い")
shutil.copy(ENV, r"C:\Users\cocor\AppData\Local\Temp\claude\C--claude\e6715bf0-b204-48d2-9e12-03d2c4fbc68b\scratchpad\env.bak_" + datetime.now().strftime("%Y%m%d%H%M%S"))
text = re.sub(rf"^{key}=.*$", f"{key}={val}", text, flags=re.M)
open(ENV, "w", encoding="utf-8").write(text)
subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value ' '"])
print(f"{key} を更新した（末尾4文字 …{val[-4:]}）。クリップボードは消した")
