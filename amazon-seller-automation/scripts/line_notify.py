"""カメラ物販 Bot 経由でかおるの LINE に通知する（C:\\claude\\.env の MERCHANDISE_LINE_TOKEN / MERCHANDISE_LINE_USER_ID）

使い方: python line_notify.py "<題名>" "<本文>"
"""
import sys
from pathlib import Path

import requests


def env(key):
    for line in Path(r"C:\claude\.env").read_text(encoding="utf-8").splitlines():
        k, _, v = line.strip().partition("=")
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return ""


def main():
    title, body = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""
    text = f"{title}\n\n{body}"[:4500]
    r = requests.post("https://api.line.me/v2/bot/message/push",
                      headers={"Authorization": f"Bearer {env('MERCHANDISE_LINE_TOKEN')}"},
                      json={"to": env("MERCHANDISE_LINE_USER_ID"), "messages": [{"type": "text", "text": text}]},
                      timeout=15)
    print("LINE 送信", "OK" if r.ok else f"失敗 HTTP {r.status_code} {r.text[:200]}")


if __name__ == "__main__":
    main()
