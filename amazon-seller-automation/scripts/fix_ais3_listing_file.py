"""AIS3 が作った出品ファイル（〇〇shipping.tsv）のリードタイム・在庫数・説明文を今のルールに直す（You and Me）

使い方: python fix_ais3_listing_file.py <出品ファイル.tsv> [...]
  → 同じフォルダに <元の名前>_fixed.tsv を書き出す（元ファイルはそのまま）

ルール（2026-10-31 かおる決定）:
  - リードタイム: 仕入れ先の日数が 0〜7 → 20日、8〜13 → 25日、14〜 → 30日
    旧設定の値から逆算する（FBA 5/6/12/18・マーチャント 6/8/13/21）
  - 在庫数: 2
  - 商品の状態の説明: 「お届けまでに10日～20日程」→「3週間～5週間程」
"""
import sys
from collections import Counter
from pathlib import Path

ENC = "cp932"
LABEL_ROW, ATTR_ROW, DATA_ROW = 3, 4, 6  # 0始まり（ファイルの settings 行の labelRow=4 / attributeRow=5 / dataRow=7）
LEAD_MAP = {5: 20, 6: 20, 8: 20, 12: 25, 13: 25, 18: 30, 21: 30, 20: 20, 25: 25, 30: 30}
QTY = "2"
NOTE_OLD, NOTE_NEW = "お届けまでに10日～20日程", "お届けまでに3週間～5週間程"


def fix(path: Path):
    lines = path.read_bytes().decode(ENC).split("\r\n")
    attrs = lines[ATTR_ROW].split("\t")
    i_qty = attrs.index("fulfillment_availability#1.quantity")
    i_lead = attrs.index("fulfillment_availability#1.lead_time_to_ship_max_days")
    i_note = attrs.index("condition_note#1.value")

    before, after, unknown = Counter(), Counter(), Counter()
    n = 0
    for k in range(DATA_ROW, len(lines)):
        if not lines[k].strip():
            continue
        c = lines[k].split("\t")
        old = int(c[i_lead]) if c[i_lead].strip() else 0
        new = LEAD_MAP.get(old)
        if new is None:
            unknown[old] += 1
            new = 30 if old > 13 else max(old, 20)
        before[old] += 1
        after[new] += 1
        c[i_lead] = str(new)
        c[i_qty] = QTY
        c[i_note] = c[i_note].replace(NOTE_OLD, NOTE_NEW)
        lines[k] = "\t".join(c)
        n += 1

    out = path.with_name(path.stem + "_fixed" + path.suffix)
    out.write_bytes("\r\n".join(lines).encode(ENC))
    print(f"{path.name}: {n}件 → {out.name}")
    print(f"  リードタイム 前 {dict(sorted(before.items()))} → 後 {dict(sorted(after.items()))}")
    if unknown:
        print(f"  ※想定外のリードタイム {dict(unknown)}（14以上は30、それ以外は20以上に寄せた）")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        fix(Path(p))
