"""AIS3 の ASIN登録に送る ASIN を、5,000件ずつの束（ais3_weekly/queue/）で順番待ちさせる

AIS3 は処理待ちのジョブ（1ジョブ5,000件）が30件までしか入らない。20万件を一度に送ると
はみ出すので、束にして置いておき、空きができた分だけまとめて送る。

使い方:
  python ais3_queue.py add <ASINのcsv>    束に分けて順番待ちに足す
  python ais3_queue.py take <空きのジョブ数>  先頭から最大その数の束を1つの CSV（ais3_weekly/upload_now.csv）にまとめる
  python ais3_queue.py done               take した束を順番待ちから消す（AIS3 に登録できたのを確かめてから）
  python ais3_queue.py status             残りの束と ASIN 数
"""
import json
import sys
from datetime import datetime
from pathlib import Path

CHUNK = 5000
BASE = Path(__file__).resolve().parents[1] / "ais3_weekly"
QUEUE = BASE / "queue"
TAKEN = QUEUE / "_taken.json"
OUT = BASE / "upload_now.csv"


def read_asins(path):
    return [s.strip().lstrip("﻿") for s in Path(path).read_text(encoding="utf-8-sig").splitlines() if s.strip()]


def chunks():
    return sorted(QUEUE.glob("*.csv"))


def add(path):
    QUEUE.mkdir(parents=True, exist_ok=True)
    asins = read_asins(path)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for i in range(0, len(asins), CHUNK):
        (QUEUE / f"{stamp}_{i // CHUNK:03d}.csv").write_bytes(("\r\n".join(asins[i:i + CHUNK]) + "\r\n").encode("utf-8"))
    print(f"順番待ちに追加: {len(asins)}件（{-(-len(asins) // CHUNK)}束）")
    status()


def take(room):
    picked = chunks()[:max(room, 0)]
    asins = [a for p in picked for a in read_asins(p)]
    if not asins:
        print("送るものなし")
        OUT.unlink(missing_ok=True)
        return
    OUT.write_bytes(("﻿" + "\r\n".join(asins) + "\r\n").encode("utf-8"))
    TAKEN.write_text(json.dumps([p.name for p in picked]), encoding="utf-8")
    print(f"{OUT} に {len(asins)}件（{len(picked)}束）をまとめた。登録できたら done を実行")


def done():
    names = json.loads(TAKEN.read_text(encoding="utf-8")) if TAKEN.exists() else []
    for n in names:
        (QUEUE / n).unlink(missing_ok=True)
    TAKEN.unlink(missing_ok=True)
    OUT.unlink(missing_ok=True)
    print(f"{len(names)}束を順番待ちから消した")
    status()


def status():
    cs = chunks()
    print(f"順番待ち: {len(cs)}束 / {sum(len(read_asins(p)) for p in cs)}件")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"add": lambda: add(sys.argv[2]), "take": lambda: take(int(sys.argv[2])), "done": done, "status": status}[cmd]()
